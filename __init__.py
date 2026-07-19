"""
BDH Bridge — Bidirectional Hermes ↔ BDH Graph Harness integration.

Write path: feeds session content to BDH after each API response.
Read path: provides bdh_query and bdh_stats tools.

v0.5.0:
  - LLM-based query classification + rewrite before BDH retrieval
  - Semantic gate replaces mechanical eligibility check (knowledge vs operational noise)
  - Context recovery from conversation_history (last N messages, no state.db needed)
  - Write path uses the rewritten query as embedding seed (read/write consistency)
  - Classification=false skips both read AND write (anti-vault-pollution)
  - Feature flag BDH_QUERY_REWRITE_ENABLED (default: false, opt-in)
  - Fallback to mechanical gate + raw query on LLM timeout/parse failure

v0.4.0:
  - Conditional automatic read-only retrieval in pre_llm_call
  - Ephemeral BDH context injection via the Hermes hook contract
  - learn=false/respond=false retrieval path avoids Hebbian updates and synthesis LLM
  - Temporary [BDH] marker when bdh_query is actually used
  - Short timeouts for tool path (30s, 1 retry) — no more 6-min agent block
  - No retry on timeout for POST /api/query (prevents double plasticity/neurogenesis)
  - try/except in all hooks with list-content handling (Anthropic block format)
  - Anti-echo-loop: skip write entirely if no user message captured
  - BDH_API configurable via BDH_API_URL env var
  - Removed dead _write_queue/_queue_lock
"""

import json
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from urllib.error import URLError

logger = logging.getLogger("bdh-bridge")

# Configurable BDH API URL (env var BDH_API_URL overrides default)
BDH_API = os.environ.get("BDH_API_URL", "http://localhost:8643")


# ---------------------------------------------------------------------------
# State: capture user message from pre_llm_call for write path
# ---------------------------------------------------------------------------

_last_user_message = ""
_last_rewritten_query = ""       # rewritten query from classification (write path consistency)
_last_should_query = None       # classification result (None = no classification done)
_bdh_used_sessions = set()
_bdh_state_lock = threading.Lock()
_AUTO_RETRIEVAL_MIN_SCORE = 0.30
# Cron jobs are operational by default. A job must opt in explicitly in its
# own prompt before the bridge may read from or write to BDH.
BDH_CRON_OPT_IN_MARKER = "[BDH:ALLOW-CRON]"
_PROMPT_BLACKLIST_FILE = Path(
    os.environ.get(
        "BDH_PROMPT_BLACKLIST_FILE",
        Path(__file__).with_name("prompt_blacklist.txt"),
    )
)

# ---------------------------------------------------------------------------
# Query rewrite pipeline config (v0.5.0)
# ---------------------------------------------------------------------------

_QUERY_REWRITE_ENABLED = os.environ.get("BDH_QUERY_REWRITE_ENABLED", "").lower() in (
    "1", "true", "yes", "on",
)
_REWRITE_MODEL = os.environ.get("BDH_REWRITE_MODEL", "deepseek-v4-flash")
_REWRITE_TIMEOUT = int(os.environ.get("BDH_REWRITE_TIMEOUT", "5"))
_REWRITE_API_URL = os.environ.get("BDH_REWRITE_API_URL", "https://ollama.com/v1")
_REWRITE_API_KEY = os.environ.get(
    "BDH_REWRITE_API_KEY",
    os.environ.get("OLLAMA_API_KEY", ""),
)
_REWRITE_HTTP_REFERER = os.environ.get("BDH_REWRITE_HTTP_REFERER", "")
_REWRITE_APP_TITLE = os.environ.get("BDH_REWRITE_APP_TITLE", "BDH Hermes Bridge")
_CONTEXT_MESSAGES_N = int(os.environ.get("BDH_CONTEXT_MESSAGES_N", "6"))
_CONTEXT_MSG_MAX_CHARS = int(os.environ.get("BDH_CONTEXT_MSG_MAX_CHARS", "200"))

_REWRITE_SYSTEM_PROMPT = (
    "You are a query router for a personal knowledge graph.\n"
    "The graph stores: concepts, decisions, architecture choices, "
    "project context, lessons learned, strategies, and factual "
    "knowledge about the user's projects and workflow. It does NOT "
    "store operational commands, system diagnostics, or transient "
    "task status.\n"
    "Given the user message and recent conversation context, decide:\n"
    "1. Does this message contain knowledge that connects to other "
    "concepts already in the graph? (decisions, explanations, facts, "
    "strategies, architecture, lessons — NOT commands, acks, "
    "diagnostics, or status)\n"
    "2. If yes, rewrite it as a clear, search-friendly query.\n"
    "3. If the message covers multiple topics, generate sub-queries.\n"
    "Reply as JSON:\n"
    '{"should_query": true|false, "query": "...", "sub_queries": ["...", "..."]}'
)


def _is_prompt_blacklisted(message):
    """Return True when a prompt is operational/meta text excluded from BDH.

    The blacklist is intentionally file-backed so it can be edited without a
    code change or restart. Empty lines and lines beginning with ``#`` are
    ignored. Entries are case-insensitive literal substrings by default;
    entries prefixed with ``re:`` are treated as regular expressions.
    """
    if not isinstance(message, str) or not message.strip():
        return False
    try:
        lines = _PROMPT_BLACKLIST_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    text = message.casefold()
    for raw_line in lines:
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.casefold().startswith("re:"):
            try:
                if re.search(entry[3:], message, flags=re.IGNORECASE | re.DOTALL):
                    return True
            except re.error:
                logger.warning("[bdh-bridge] invalid blacklist regex ignored: %s", entry)
        elif entry.casefold() in text:
            return True
    return False


def _is_cron_source(platform=None, source=None):
    """Return True for Hermes scheduled-agent hook calls."""
    value = platform or source or ""
    return str(value).strip().casefold() == "cron"


def _cron_has_bdh_opt_in(message):
    """Allow BDH for a cron only when its own prompt opts in explicitly."""
    return isinstance(message, str) and BDH_CRON_OPT_IN_MARKER in message


def _turn_key(kwargs):
    """Return the stable key shared by tool and output hooks for this turn."""
    return kwargs.get("session_id") or kwargs.get("task_id")


# ---------------------------------------------------------------------------
# BDH HTTP helpers
# ---------------------------------------------------------------------------

def _bdh_request(endpoint, data=None, timeout=10, retries=1, backoff_base=2.0,
                 retry_on_timeout=True):
    """HTTP request to BDH API with optional retry + exponential backoff.

    Args:
        retry_on_timeout: If False, do NOT retry on timeout errors. This is
            critical for POST /api/query — if the server processed the request
            but the client timed out, retrying would re-run plasticity and
            neurogenesis, causing double learning and duplicate notes.

    Returns response dict on success, None after all retries exhausted.
    """
    url = f"{BDH_API}{endpoint}"
    last_error = None

    for attempt in range(retries):
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"},
                )
            else:
                req = urllib.request.Request(url)

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())

        except (URLError, OSError, json.JSONDecodeError) as e:
            last_error = e
            # Don't retry on timeout for POST requests (non-idempotent)
            if not retry_on_timeout and isinstance(e, URLError):
                reason = getattr(e, 'reason', '')
                if 'timed out' in str(reason).lower() or 'timeout' in str(reason).lower():
                    logger.warning(
                        f"[bdh-bridge] timeout on {endpoint} (not retrying — "
                        f"non-idempotent POST)"
                    )
                    return None
            if attempt < retries - 1:
                wait = backoff_base ** attempt
                logger.warning(
                    f"[bdh-bridge] request failed (attempt {attempt + 1}/{retries}): {e} "
                    f"— retrying in {wait}s"
                )
                time.sleep(wait)

    logger.error(f"[bdh-bridge] all {retries} retries exhausted for {endpoint}: {last_error}")
    return None


def _bdh_query_sync(query_text, user_prompt=None, source=None, timeout=30,
                    learn=True, retries=2, vault_id=None):
    """Synchronous query to BDH.

    ``learn=False`` is used by automatic pre-LLM retrieval: it must provide
    context without changing Hebbian state or running neurogenesis.
    """
    payload = {
        "query": query_text,
        "learn": learn,
        "respond": not (source == "automatic_retrieval" and not learn),
    }
    # Omit vault_id when not explicitly selected: BDH resolves its configured
    # default_vault. Routing policy remains outside this low-level helper.
    if vault_id:
        payload["vault_id"] = vault_id
    if user_prompt:
        payload["user_prompt"] = user_prompt
    if source:
        payload["source"] = source
    return _bdh_request("/api/query", payload, timeout=timeout, retries=retries,
                        retry_on_timeout=False)


def _should_auto_retrieve(message):
    """Skip trivial chatter; retrieve for any substantive user message.

    This deliberately avoids a domain keyword list. BDH is domain-agnostic, and
    a vocabulary gate would miss synonyms, other languages, and new concepts.
    """
    if not isinstance(message, str):
        return False
    text = message.strip()
    if len(text) < 24:
        return False

    normalized = text.casefold()
    casual = {
        "ciao", "hello", "hi", "ok", "okay", "thanks", "thank you",
        "grazie", "perfetto", "va bene", "bene", "sì", "si", "no",
    }
    if normalized.rstrip(".!?") in casual:
        return False

    # Avoid retrieval for pure acknowledgements with no information request.
    if re.fullmatch(
        r"(?:ok|okay|va bene|bene|perfetto|grazie|thanks|capito|ricevuto)"
        r"(?:[.! ]+|$)",
        normalized,
    ):
        return False

    # Questions are eligible even when short; longer messages are treated as
    # substantive without trying to guess their domain from keywords.
    return "?" in text or len(text) >= 40


def _has_relevant_bdh_context(result):
    """Use raw Hybrid routing metadata as the semantic routing gate."""
    if not isinstance(result, dict):
        return False

    routing = result.get("routing")
    if isinstance(routing, dict):
        hybrid = float(routing.get("hybrid_top_score", 0.0))
        vector = float(routing.get("vector_top_score", 0.0))
        matched = int(routing.get("bm25_matched_term_count", 0) or 0)
        # Prefer lexical evidence; allow a strong semantic match for novel
        # concepts that are not named exactly in the vault.
        return (
            hybrid >= _AUTO_RETRIEVAL_MIN_SCORE
            and (matched >= 2 or vector >= 0.50)
        )

    # Backward-compatible fallback for older BDH servers.
    scores = [
        float(note.get("score", 0.0))
        for note in (result.get("activated_notes") or [])
        if isinstance(note, dict)
    ]
    return bool(scores) and max(scores) >= _AUTO_RETRIEVAL_MIN_SCORE


def _format_bdh_context(result):
    """Format BDH retrieval as ephemeral, clearly delimited model context."""
    if not isinstance(result, dict):
        return ""
    notes = result.get("activated_notes") or []
    synthesis = (result.get("response") or "").strip()
    if not notes and not synthesis:
        return ""

    lines = ["[BDH CONTEXT — optional]"]
    if notes:
        lines.append("Activated neurons:")
        for note in notes[:8]:
            title = note.get("title", note.get("id", "unknown"))
            score = note.get("score")
            suffix = f" (score: {score})" if score is not None else ""
            lines.append(f"- {title}{suffix}")
    if synthesis:
        lines.extend(["", "Relevant graph synthesis:", synthesis[:4000]])
    lines.extend([
        "", "Use this as supporting context.",
        "Do not mention BDH unless relevant.",
        "If it conflicts with the current conversation, prefer the current conversation.",
        "[/BDH CONTEXT]",
    ])
    return "\n".join(lines)


def _bdh_query_async(query_text, user_prompt=None, source="assistant_response"):
    """Fire-and-forget query — used by hooks.

    Short timeout (30s) and 1 retry. If BDH is down, the daemon thread
    exits quickly instead of piling up.
    """
    def _worker():
        payload = {"query": query_text}
        if user_prompt:
            payload["user_prompt"] = user_prompt
        if source:
            payload["source"] = source

        result = _bdh_request("/api/query", payload, timeout=30, retries=2,
                               retry_on_timeout=False)
        if result:
            new = result.get("new_concepts", [])
            activated = len(result.get("activated_notes", []))
            hebbian = len(result.get("hebbian_updates", []))
            if new:
                logger.info(
                    f"[bdh-bridge] neurogenesis: {len(new)} new concepts "
                    f"({activated} activated, {hebbian} hebbian updates)"
                )
        else:
            logger.warning("[bdh-bridge] BDH unreachable — consolidation or server down")

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Query rewrite pipeline (v0.5.0)
# ---------------------------------------------------------------------------

def _extract_context(conversation_history, n=_CONTEXT_MESSAGES_N,
                     max_chars=_CONTEXT_MSG_MAX_CHARS):
    """Extract last N messages from conversation_history as compact text.

    Hermes passes conversation_history in pre_llm_call kwargs. Each entry
    is typically a dict with 'role' and 'content' keys. We extract just the
    text, truncated, to give the rewrite LLM enough context without flooding it.
    """
    if not isinstance(conversation_history, list) or not conversation_history:
        return ""

    lines = []
    # Take the last n messages
    for msg in conversation_history[-n:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = msg.get("content", "")
        # Handle Anthropic block format
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        elif not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not content:
            continue
        lines.append(f"[{role}] {content[:max_chars]}")

    return "\n".join(lines)


def _rewrite_query(user_message, context_text=""):
    """Call the rewrite LLM to classify + rewrite the user message.

    Returns a dict with:
      - should_query: bool (whether BDH should be queried)
      - query: str (rewritten query, or original if fallback)
      - sub_queries: list[str] (additional queries if multi-topic)

    On any failure (timeout, parse error, network), returns None so the
    caller falls back to the mechanical gate + raw user message.
    """
    if not _REWRITE_API_KEY:
        logger.debug("[bdh-bridge] rewrite skipped — no OLLAMA_API_KEY set")
        return None

    user_content = f"User message:\n{user_message[:1500]}"
    if context_text:
        user_content += f"\n\nRecent context:\n{context_text}"

    payload = {
        "model": _REWRITE_MODEL,
        "messages": [
            {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "stream": False,
        # Force JSON response format if the API supports it
        "format": "json",
    }

    url = f"{_REWRITE_API_URL}/chat/completions"
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_REWRITE_API_KEY}",
    }
    if _REWRITE_HTTP_REFERER:
        headers["HTTP-Referer"] = _REWRITE_HTTP_REFERER
    if _REWRITE_APP_TITLE:
        headers["X-Title"] = _REWRITE_APP_TITLE

    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=_REWRITE_TIMEOUT) as resp:
            data = json.loads(resp.read())

        # OpenAI-compatible response format
        choice = data.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")

        # Strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        result = json.loads(content)

        should_query = bool(result.get("should_query", True))
        query = result.get("query", "").strip() or user_message
        sub_queries = [
            sq.strip() for sq in result.get("sub_queries", [])
            if isinstance(sq, str) and sq.strip()
        ]

        logger.info(
            f"[bdh-bridge] rewrite: should_query={should_query}, "
            f"query={query[:80]!r}, sub_queries={len(sub_queries)}"
        )
        return {
            "should_query": should_query,
            "query": query,
            "sub_queries": sub_queries,
        }

    except (URLError, OSError) as e:
        reason = getattr(e, 'reason', '')
        if 'timed out' in str(reason).lower() or 'timeout' in str(reason).lower():
            logger.debug(f"[bdh-bridge] rewrite LLM timeout ({_REWRITE_TIMEOUT}s) — fallback to raw")
        else:
            logger.debug(f"[bdh-bridge] rewrite LLM error: {e} — fallback to raw")
        return None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.debug(f"[bdh-bridge] rewrite LLM parse error: {e} — fallback to raw")
        return None


# ---------------------------------------------------------------------------
# Hook: pre_llm_call — capture user message for write path context
# ---------------------------------------------------------------------------

def _on_pre_llm_call(**kwargs):
    """Capture the user message and optionally retrieve read-only BDH context.

    v0.5.0: when BDH_QUERY_REWRITE_ENABLED is true, the hook first calls an
    LLM to classify + rewrite the user message. If the LLM says should_query=false,
    both read and write paths are skipped (anti-vault-pollution). On any LLM
    failure, it falls back to the mechanical gate + raw user message.
    """
    global _last_user_message, _last_rewritten_query, _last_should_query
    try:
        # A new user turn starts here. Clear the previous debug marker so the
        # tag cannot leak into the next answer in a long-lived session.
        key = _turn_key(kwargs)
        if key is not None:
            with _bdh_state_lock:
                _bdh_used_sessions.discard(str(key))

        msg = kwargs.get("user_message", "")
        if not isinstance(msg, str) or not msg.strip():
            return None
        _last_user_message = msg
        _last_rewritten_query = ""       # reset per turn
        _last_should_query = None        # reset per turn

        if _is_cron_source(kwargs.get("platform"), kwargs.get("source")) and not _cron_has_bdh_opt_in(msg):
            logger.info("[bdh-bridge] automatic retrieval skipped — cron source is deny-by-default")
            return None

        if _is_prompt_blacklisted(msg):
            logger.info("[bdh-bridge] automatic retrieval skipped — prompt is blacklisted")
            return None

        # ── Query rewrite pipeline ──────────────────────────────────────
        if _QUERY_REWRITE_ENABLED:
            # Extract context from conversation_history
            context_text = _extract_context(kwargs.get("conversation_history"))

            # Call the rewrite LLM (classify + rewrite in one shot)
            rewrite_result = _rewrite_query(msg, context_text)

            if rewrite_result is not None:
                _last_should_query = rewrite_result["should_query"]
                _last_rewritten_query = rewrite_result["query"]

                if not rewrite_result["should_query"]:
                    logger.info("[bdh-bridge] rewrite: LLM classified as non-knowledge — skip read+write")
                    return None

                # Use the rewritten query for BDH retrieval
                bdh_query = rewrite_result["query"][:1500]
            else:
                # Fallback: LLM failed, use mechanical gate + raw message
                if not _should_auto_retrieve(msg):
                    return None
                bdh_query = msg[:1500]
        else:
            # Feature flag off: use mechanical gate + raw message (v0.4.0 behavior)
            if not _should_auto_retrieve(msg):
                return None
            bdh_query = msg[:1500]

        # ── BDH retrieval (read-only) ──────────────────────────────────
        result = _bdh_query_sync(
            bdh_query,
            source="automatic_retrieval",
            timeout=2,
            learn=False,
            retries=1,
        )
        context = _format_bdh_context(result) if _has_relevant_bdh_context(result) else ""
        if context:
            logger.info("[bdh-bridge] automatic retrieval: context injected")
            return {"context": context}
        logger.debug("[bdh-bridge] automatic retrieval: below relevance threshold")
    except Exception as e:
        logger.debug(f"[bdh-bridge] pre_llm_call error: {e}")
    return None

# ---------------------------------------------------------------------------
# Hook: post_api_request — feed BDH after each final response
# ---------------------------------------------------------------------------

def _on_post_api_request(**kwargs):
    """After the LLM finishes a response, query BDH with the assistant content.

    Only fires on final responses (finish_reason == "stop") and only when
    the response is substantial enough to be worth querying.

    Sends source="assistant_response" so BDH can dampen Hebbian learning
    and prevent echo-loop reinforcement.

    Anti-echo-loop: if no user message was captured (pre_llm_call didn't fire),
    SKIP the write entirely — embedding the assistant response alone would
    reinforce existing connections rather than discovering new ones.
    """
    try:
        finish_reason = kwargs.get("finish_reason", "")
        if finish_reason != "stop":
            return

        if _is_cron_source(kwargs.get("platform"), kwargs.get("source")) and not _cron_has_bdh_opt_in(_last_user_message):
            logger.info("[bdh-bridge] write skipped — cron source is deny-by-default")
            return

        content_chars = kwargs.get("assistant_content_chars", 0) or 0
        if content_chars < 200:
            return  # skip trivial responses ("done", "ok", "pushato")

        assistant_msg = kwargs.get("assistant_message")
        if not assistant_msg:
            return

        # Handle both string content and list-of-blocks content (Anthropic format)
        raw_content = getattr(assistant_msg, "content", "")
        if isinstance(raw_content, list):
            # Extract text from content blocks
            text = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in raw_content
            )
        elif isinstance(raw_content, str):
            text = raw_content
        else:
            text = str(raw_content)

        if not text.strip():
            return

        # Anti-echo-loop: require a user message to proceed.
        # Without the user's question, embedding the assistant response alone
        # would create echo loops (finding notes similar to the response,
        # reinforcing existing connections instead of discovering new ones).
        if not _last_user_message or not _last_user_message.strip():
            logger.debug("[bdh-bridge] skipping write — no user message captured")
            return

        if _is_prompt_blacklisted(_last_user_message):
            logger.info("[bdh-bridge] write skipped — prompt is blacklisted")
            return

        # v0.5.0: if classification said should_query=false, skip the write too.
        # This prevents operational noise from polluting the vault via the
        # write path even when the read path was already skipped.
        if _last_should_query is False:
            logger.info("[bdh-bridge] write skipped — LLM classified as non-knowledge")
            return

        # Use the USER MESSAGE as the embedding seed (query) — that's the signal.
        # The assistant response is passed as user_prompt for LLM/neurogenesis context.
        # v0.5.0: prefer the rewritten query for read/write consistency.
        query = (_last_rewritten_query or _last_user_message)[:1500]
        user_prompt = text[:1500]

        _bdh_query_async(query, user_prompt=user_prompt, source="assistant_response")

    except Exception as e:
        logger.warning(f"[bdh-bridge] post_api_request hook error: {e}")


# ---------------------------------------------------------------------------
# Hooks: detect actual BDH tool use and mark the final answer for debugging
# ---------------------------------------------------------------------------

def _on_post_tool_call(**kwargs):
    """Remember successful bdh_query use for the current turn."""
    try:
        if kwargs.get("tool_name") != "bdh_query":
            return

        result = kwargs.get("result", "") or ""
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        else:
            payload = result if isinstance(result, dict) else {}

        # Do not claim BDH was used if the tool only returned an error.
        if not isinstance(payload, dict) or payload.get("error"):
            return

        key = _turn_key(kwargs)
        if key is not None:
            with _bdh_state_lock:
                _bdh_used_sessions.add(str(key))
    except Exception as e:
        logger.debug(f"[bdh-bridge] post_tool_call debug marker error: {e}")


def _on_transform_llm_output(**kwargs):
    """Prepend a temporary [BDH] marker when bdh_query fed the answer."""
    try:
        text = kwargs.get("response_text")
        if not isinstance(text, str):
            return None

        key = _turn_key(kwargs)
        with _bdh_state_lock:
            used = key is not None and str(key) in _bdh_used_sessions

        if used and not text.startswith("[BDH]"):
            return f"[BDH] {text}"
    except Exception as e:
        logger.debug(f"[bdh-bridge] transform_llm_output debug marker error: {e}")
    return None


# ---------------------------------------------------------------------------
# Tool: bdh_query — query BDH graph for context
# ---------------------------------------------------------------------------

def _tool_bdh_query(args, **kwargs):
    """Query the BDH knowledge graph.

    Args:
        query: The question or topic to search for in the knowledge graph.

    Returns:
        JSON with activated_notes, response, new_concepts, hebbian_updates.
    """
    try:
        query = args.get("query", "").strip()
        if not query:
            return json.dumps({"error": "Missing 'query' parameter"})

        result = _bdh_query_sync(
            query,
            source="hermes_tool",
            vault_id=args.get("vault_id") or None,
        )
        if result is None:
            return json.dumps({
                "error": "BDH server unreachable — possibly in consolidation. "
                         "Answer using your internal knowledge."
            })

        # Format for LLM consumption
        output = {
            "activated_notes": [
                {"id": n["id"], "title": n["title"], "score": round(n["score"], 3)}
                for n in result.get("activated_notes", [])[:10]
            ],
            "response": result.get("response", ""),
            "new_concepts": result.get("new_concepts", []),
            "hebbian_updates_count": len(result.get("hebbian_updates", [])),
            "neuron_count": result.get("neuron_count", 0),
            "synapse_count": result.get("synapse_count", 0),
        }
        return json.dumps(output, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"bdh_query tool error: {e}"})


# ---------------------------------------------------------------------------
# Tool: bdh_stats — quick graph stats
# ---------------------------------------------------------------------------

def _tool_bdh_stats(args, **kwargs):
    """Get current BDH graph statistics.

    Returns:
        JSON with neuron count, active/dormant, synapses, hebbian, avg_degree.
    """
    try:
        vault_id = args.get("vault_id") if isinstance(args, dict) else None
        endpoint = "/api/stats"
        if vault_id:
            endpoint += "?vault_id=" + urllib.parse.quote(vault_id, safe="")
        result = _bdh_request(endpoint, timeout=5, retries=1)
        if result is None:
            return json.dumps({"error": "BDH server unreachable"})
        return json.dumps({
            "neurons": result.get("neurons", 0),
            "active_neurons": result.get("active_neurons", 0),
            "dormant_neurons": result.get("dormant_neurons", 0),
            "synapses": result.get("synapses", 0),
            "hebbian_synapses": result.get("hebbian_synapses", 0),
            "avg_degree": round(result.get("avg_degree", 0), 2),
            "queries_processed": result.get("queries_processed", 0),
        })
    except Exception as e:
        return json.dumps({"error": f"bdh_stats tool error: {e}"})


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register hooks and tools with the Hermes plugin context."""
    # Hook: capture user message before LLM call
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # Hook: feed BDH after each API response
    ctx.register_hook("post_api_request", _on_post_api_request)

    # Temporary debug marker: show when the model actually used bdh_query.
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)

    # Tools: query BDH and get stats
    ctx.register_tool(
        "bdh_query",
        "bdh",
        {
            "name": "bdh_query",
            "description": "Query the BDH knowledge graph. Returns activated neurons, "
                           "LLM response, and any new concepts created via neurogenesis. "
                           "Use when you need context from the BDH graph about a topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question or topic to search for in the knowledge graph."
                    },
                    "vault_id": {
                        "type": "string",
                        "description": "Optional vault ID. Omit to use BDH's configured default vault."
                    }
                },
                "required": ["query"]
            }
        },
        _tool_bdh_query,
    )
    ctx.register_tool(
        "bdh_stats",
        "bdh",
        {
            "name": "bdh_stats",
            "description": "Get current BDH graph statistics: neuron count, active/dormant, "
                           "synapses, hebbian links, average degree.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vault_id": {
                        "type": "string",
                        "description": "Optional vault ID. Omit to use BDH's configured default vault."
                    }
                }
            }
        },
        _tool_bdh_stats,
    )

    logger.info(
        "[bdh-bridge] registered: hooks=[pre_llm_call, post_api_request, "
        "post_tool_call, transform_llm_output], "
        f"tools=[bdh_query, bdh_stats], api={BDH_API}"
    )