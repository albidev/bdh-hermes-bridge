"""
BDH Bridge — Bidirectional Hermes ↔ BDH Graph Harness integration.

Write path: feeds session content to BDH after each API response.
Read path: provides bdh_query and bdh_stats tools.

v0.3.0:
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
import threading
import time
import urllib.request
from urllib.error import URLError

logger = logging.getLogger("bdh-bridge")

# Configurable BDH API URL (env var BDH_API_URL overrides default)
BDH_API = os.environ.get("BDH_API_URL", "http://localhost:8643")


# ---------------------------------------------------------------------------
# State: capture user message from pre_llm_call for write path
# ---------------------------------------------------------------------------

_last_user_message = ""


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


def _bdh_query_sync(query_text, user_prompt=None, source=None, timeout=30):
    """Synchronous query to BDH — used by bdh_query tool.

    Short timeout (30s) and NO retry — this is a tool call from the agent,
    so blocking for 6 minutes is unacceptable. If BDH is down, the agent
    should fall back to its own knowledge.
    """
    payload = {"query": query_text}
    if user_prompt:
        payload["user_prompt"] = user_prompt
    if source:
        payload["source"] = source
    # 1 retry only (so 2 total attempts), no retry on timeout
    return _bdh_request("/api/query", payload, timeout=timeout, retries=2,
                         retry_on_timeout=False)


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
# Hook: pre_llm_call — capture user message for write path context
# ---------------------------------------------------------------------------

def _on_pre_llm_call(**kwargs):
    """Before the LLM generates, capture the user message.

    This is used by post_api_request to include user context in BDH payloads,
    enabling proper question→answer synaptic associations.
    """
    global _last_user_message
    try:
        msg = kwargs.get("user_message", "")
        if msg and isinstance(msg, str):
            _last_user_message = msg
    except Exception as e:
        logger.debug(f"[bdh-bridge] pre_llm_call error: {e}")


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

        # Use the USER MESSAGE as the embedding seed (query) — that's the signal.
        # The assistant response is passed as user_prompt for LLM/neurogenesis context.
        query = _last_user_message[:1500]
        user_prompt = text[:1500]

        _bdh_query_async(query, user_prompt=user_prompt, source="assistant_response")

    except Exception as e:
        logger.warning(f"[bdh-bridge] post_api_request hook error: {e}")


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

        result = _bdh_query_sync(query, source="hermes_tool")
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
        result = _bdh_request("/api/stats", timeout=5, retries=1)
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
            "parameters": {"type": "object", "properties": {}}
        },
        _tool_bdh_stats,
    )

    logger.info(
        "[bdh-bridge] registered: hooks=[pre_llm_call, post_api_request], "
        f"tools=[bdh_query, bdh_stats], api={BDH_API}"
    )