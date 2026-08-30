"""
BDH Bridge — Bidirectional Hermes ↔ BDH Graph Harness integration.

Write path: feeds session content to BDH after each API response.
Read path: provides bdh_query and bdh_stats tools.

v0.8.0:
  - Independent retrieval/storage routing contract (schema_version=2)
  - Strict boolean validation prevents string `"false"` from failing open
  - Context recovery filters system/tool messages and deduplicates the current turn
  - v2 retrieval variants capped at three
  - Truncated (`finish_reason=length`) responses are not learned

v0.7.1:
  - Session-end synthesis: accumulate written turns per session and, on
    session identity lifecycle (finalize/reset), flush a single curated
    synthesis so BDH learns the insight that emerges across the whole
    conversation, not just per-turn noise
  - Uses Hermes on_session_finalize / on_session_reset hooks instead of
    process-global session-rotation inference (#14)
  - Each session buffer is flushed at most once via _flushed_sessions guard
  - Opt-in via BDH_SESSION_SYNTH_ENABLED=true (default off)
  - BDH_SESSION_SYNTH_MIN_TURNS (default 3), BDH_SESSION_SYNTH_MAX_CHARS (default 6000)

v0.6.0:
  - Language-agnostic multi-query retrieval bridge normalization (#19)
  - Optional provider-specific arrays `search_queries`/`query_variants` are
    normalized into a provider-neutral `sub_queries` list
  - Original `query` is always preserved for the write path
  - Legacy single `search_query` field still works unchanged
  - Validates variants for emptiness, duplicates, and a configurable max limit
  - Invalid rewrite output still falls back to mechanical gate + raw message

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
_DEFAULT_BDH_API = "http://localhost:8643"


def _current_bdh_api():
    """Resolve BDH_API_URL at request time after gateway configuration loads."""
    return os.environ.get("BDH_API_URL", _DEFAULT_BDH_API)


def _bounded_int(value, default, minimum=1, maximum=None):
    """Parse an integer setting without allowing import-time config failures."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(parsed, minimum)
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


# ---------------------------------------------------------------------------
# State: capture user message from pre_llm_call for write path
# ---------------------------------------------------------------------------

_TURN_STATE_TTL_SECONDS = min(
    max(int(os.environ.get("BDH_TURN_STATE_TTL_SECONDS", "300")), 1), 300
)
_TURN_STATE_MAX_ENTRIES = max(
    int(os.environ.get("BDH_TURN_STATE_MAX_ENTRIES", "1000")), 1
)
# Session-end synthesis (v0.7.1): accumulate written turns per session and, on
# Hermes session finalization/reset, send a single curated synthesis so the
# vault learns the insight that emerges across the whole conversation, not just
# per-turn noise.
_SESSION_SYNTH_ENABLED = os.environ.get("BDH_SESSION_SYNTH_ENABLED", "").lower() in (
    "1", "true", "yes", "on",
)
# A session is only worth synthesising once it has at least this many written
# turns — a 1-turn session is already covered by the per-turn write path.
_SESSION_SYNTH_MIN_TURNS = _bounded_int(
    os.environ.get("BDH_SESSION_SYNTH_MIN_TURNS", "3"), 3, maximum=100
)
# Cap the transcript we feed to the synthesis so a long session cannot blow up
# the BDH request / neurogenesis context.
_SESSION_SYNTH_MAX_CHARS = _bounded_int(
    os.environ.get("BDH_SESSION_SYNTH_MAX_CHARS", "6000"), 6000, maximum=100000
)
_turn_states = {}
_session_buffers = {}  # session_id -> list of {"user": ..., "assistant": ...}
_flushed_sessions = set()  # session_ids already flushed — never double-flush
_session_pending_writes = {}  # session_id -> number of in-flight per-turn writes
_session_finalize_requested = set()  # sessions waiting for pending writes to settle
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
# Query rewrite pipeline config (v0.8.0)
# ---------------------------------------------------------------------------

_QUERY_REWRITE_ENABLED = os.environ.get("BDH_QUERY_REWRITE_ENABLED", "").lower() in (
    "1", "true", "yes", "on",
)
_REWRITE_MODEL = os.environ.get("BDH_REWRITE_MODEL", "deepseek-v4-flash:cloud").strip() or "deepseek-v4-flash:cloud"
_REWRITE_TIMEOUT = _bounded_int(
    os.environ.get("BDH_REWRITE_TIMEOUT", "15"), 15, maximum=15
)
_REWRITE_API_URL = (
    os.environ.get("BDH_REWRITE_API_URL", "https://ollama.com/v1").strip().rstrip("/")
)
_REWRITE_API_KEY = os.environ.get(
    "BDH_REWRITE_API_KEY",
    os.environ.get("OLLAMA_API_KEY", ""),
)
_REWRITE_HTTP_REFERER = os.environ.get("BDH_REWRITE_HTTP_REFERER", "")
_REWRITE_APP_TITLE = os.environ.get("BDH_REWRITE_APP_TITLE", "BDH Hermes Bridge")
_REWRITE_PROMPT_FILE = os.environ.get("BDH_REWRITE_PROMPT_FILE", "")
_REWRITE_OPENROUTER_URL = (
    os.environ.get("BDH_REWRITE_OPENROUTER_URL", "https://openrouter.ai/api/v1")
    .strip()
    .rstrip("/")
)
_REWRITE_OPENROUTER_MODEL = (
    os.environ.get("BDH_REWRITE_OPENROUTER_MODEL", "z-ai/glm-5.2:free").strip()
    or "z-ai/glm-5.2:free"
)
_REWRITE_LOCAL_URL = (
    os.environ.get("BDH_REWRITE_LOCAL_URL", "http://127.0.0.1:8083/v1")
    .strip()
    .rstrip("/")
)
_REWRITE_LOCAL_MODEL = (
    os.environ.get("BDH_REWRITE_LOCAL_MODEL", "qwen3.8-27b-oq4e-mtp").strip()
    or "qwen3.8-27b-oq4e-mtp"
)
_CONTEXT_MESSAGES_N = _bounded_int(
    os.environ.get("BDH_CONTEXT_MESSAGES_N", "6"), 6, maximum=20
)
_CONTEXT_MSG_MAX_CHARS = _bounded_int(
    os.environ.get("BDH_CONTEXT_MSG_MAX_CHARS", "200"), 200, maximum=1000
)
# Max number of search/query variants accepted from an LLM rewrite. Extras are
# dropped; duplicates/empty strings are filtered out. This keeps the bridge
# provider-agnostic regardless of how an LLM names the array.
_REWRITE_MAX_VARIANTS = _bounded_int(
    os.environ.get("BDH_REWRITE_MAX_VARIANTS", "10"), 10, maximum=10
)

# Default rewrite prompt. Can be overridden via BDH_REWRITE_PROMPT_FILE.
_DEFAULT_REWRITE_SYSTEM_PROMPT = (
    "You are a query router for a personal knowledge graph.\n"
    "The graph stores: concepts, decisions, architecture choices, "
    "project context, lessons learned, strategies, and factual "
    "knowledge about the user's projects and workflow. It does NOT "
    "store operational commands, system diagnostics, or transient "
    "task status.\n"
    "Given the current user message and recent conversation context, decide:\n"
    "1. Does this message require knowledge retrieval from the graph?\n"
    "2. Does this interaction contain a durable knowledge candidate worth storing?\n"
    "3. If retrieval is useful, rewrite the query as a clear, search-friendly "
    "query preserving the user's language and intent.\n"
    "4. If the message covers multiple genuinely independent topics, generate "
    "at most three retrieval sub-queries.\n"
    "5. Use context only to resolve references and omitted subjects. Never "
    "invent facts, decisions, entities, or conclusions.\n"
    "6. `search_query` is retrieval-only and may use concise English/technical "
    "keywords when that improves recall; `query` remains the canonical "
    "user-language intent for storage.\n"
    "Few-shot examples for routing calibration. Apply the same principles, not literal keyword matching.\n\n"
    "Example 1 — operational noise:\n"
    'User message: "ok committa e pusha il fix"\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":false,"store_candidate":false,"query":"ok committa e pusha il fix","search_query":"","sub_queries":[],"knowledge_types":[],"confidence":0.95}\n\n'
    "Example 2 — operational acknowledgement:\n"
    'User message: "come andiamo?"\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":false,"store_candidate":false,"query":"come andiamo?","search_query":"","sub_queries":[],"knowledge_types":[],"confidence":0.9}\n\n'
    "Example 3 — knowledge question:\n"
    'User message: "Come funziona l\'apprendimento Hebbiano?"\n'
    "Output:\n"
    "{\"schema_version\":2,\"should_retrieve\":true,\"store_candidate\":false,\"query\":\"Come funziona l'apprendimento Hebbiano?\",\"search_query\":\"Hebbian learning mechanism\",\"sub_queries\":[],\"knowledge_types\":[\"concepts\"],\"confidence\":0.95}\n\n"
    "Example 4 — architecture question:\n"
    'User message: "Qual è la differenza tra il read path e il write path del bridge?"\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":true,"store_candidate":false,"query":"Differenza tra read path e write path del bridge","search_query":"BDH bridge read path vs write path","sub_queries":[],"knowledge_types":["architecture"],"confidence":0.95}\n\n'
    "Example 5 — durable decision:\n"
    'User message: "La scelta è usare il vault come substrato del grafo e Chroma solo come indice di embedding."\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":false,"store_candidate":true,"query":"La scelta è usare il vault come substrato del grafo e Chroma solo come indice di embedding.","search_query":"","sub_queries":[],"knowledge_types":["architecture choices","decisions"],"confidence":0.95}\n\n'
    "Example 6 — durable proposal:\n"
    'User message: "Aggiungiamo query classification e routing per non inquinare il vault."\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":false,"store_candidate":true,"query":"Aggiungere query classification e routing per prevenire la contaminazione del vault","search_query":"","sub_queries":[],"knowledge_types":["architecture choices","strategies"],"confidence":0.9}\n\n'
    "Example 7 — project status retrieval:\n"
    'User message: "Come avevamo risolto il problema del gateway BDH?"\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":true,"store_candidate":false,"query":"Come avevamo risolto il problema del gateway BDH?","search_query":"BDH gateway problem solution fix","sub_queries":[],"knowledge_types":["lessons learned","decisions"],"confidence":0.95}\n\n'
    "Example 8 — unrelated external link:\n"
    'User message: "Cosa ne pensi di questo? https://example.com/article"\n'
    "Output:\n"
    '{"schema_version":2,"should_retrieve":false,"store_candidate":false,"query":"Cosa ne pensi di questo? https://example.com/article","search_query":"","sub_queries":[],"knowledge_types":[],"confidence":0.8}\n\n'
    "Important calibration rule: `should_retrieve` and `store_candidate` are independent. A question normally retrieves but is not itself a durable candidate. A durable decision or proposal may be stored without retrieving first. Operational commands, acknowledgements, status checks, and transient UI/task actions are neither. Never turn a short operational message into a knowledge query merely because recent context is technical.\n"
    "Reply as JSON only:\n"
    '{"schema_version": 2, "should_retrieve": true|false, "store_candidate": true|false, "query": "...", "search_query": "...", "sub_queries": ["..."], "knowledge_types": [], "confidence": 0.0}'
)


def _normalize_knowledge_types(value):
    """Keep a small, non-authoritative list of knowledge labels for telemetry."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][:4]


def _normalize_confidence(value):
    """Return a bounded numeric confidence, or None when it is not trustworthy."""
    if isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    return confidence


def _normalize_query_variants(result, user_message, max_variants=_REWRITE_MAX_VARIANTS):
    """Normalize optional provider-specific variant fields into a clean list.

    Some rewrite providers return `search_queries` or `query_variants` instead
    of (or alongside) the legacy `search_query` and `sub_queries` fields. This
    function coalesces all of them into a provider-neutral list of non-empty,
    deduplicated, bounded variant strings.

    Behavior:
      - If `search_query` is present and non-empty, it is treated as the
        first variant (legacy behavior preserved).
      - `search_queries` and `query_variants` arrays are appended if they are
        iterable strings.
      - `sub_queries` are appended as before.
      - Empty/whitespace-only strings, non-string entries, and duplicates are
        removed.
      - The list is truncated to `max_variants`.
      - The original `query` field is NEVER modified so the write path can keep
        using the user's real intent.

    Returns a dict with the same keys as the input plus normalized
    `search_query` (first variant) and `sub_queries` (remaining variants).
    On malformed input (e.g. `query` missing/empty, non-dict result), returns
    None so the caller falls back to the raw message.
    """
    if not isinstance(result, dict):
        return None

    # v2 separates retrieval from storage. Keep accepting v1 output so a
    # provider/model update cannot break the bridge during a rolling change.
    is_v2 = (
        result.get("schema_version") == 2
        or "should_retrieve" in result
        or "store_candidate" in result
    )
    if is_v2:
        if result.get("schema_version") != 2:
            return None
        should_retrieve = result.get("should_retrieve")
        store_candidate = result.get("store_candidate")
        if not isinstance(should_retrieve, bool) or not isinstance(store_candidate, bool):
            return None
    else:
        should_retrieve = result.get("should_query", True)
        if not isinstance(should_retrieve, bool):
            return None
        # v1 coupled both decisions. Preserve that behavior only for legacy
        # payloads; new v2 payloads must choose independently.
        store_candidate = should_retrieve

    raw_query = result.get("query", "")
    if raw_query is None and not (should_retrieve or store_candidate):
        raw_query = ""
    if not isinstance(raw_query, str):
        return None
    original_query = raw_query.strip()
    if not original_query and (should_retrieve or store_candidate):
        return None
    if not original_query:
        original_query = user_message.strip() if isinstance(user_message, str) else ""

    if not should_retrieve:
        if not is_v2:
            # Preserve the exact v1 rejection shape for older callers.
            return {
                "should_query": False,
                "query": original_query,
                "search_query": original_query,
                "sub_queries": [],
            }
        return {
            "schema_version": 2,
            "should_retrieve": False,
            "store_candidate": store_candidate,
            "should_query": False,
            "query": original_query,
            "search_query": "",
            "sub_queries": [],
            "knowledge_types": _normalize_knowledge_types(result.get("knowledge_types")),
            "confidence": _normalize_confidence(result.get("confidence")),
        }

    variants = []

    # Legacy single-string field must keep working exactly as before.
    legacy_search_query = result.get("search_query", "").strip()
    if legacy_search_query:
        variants.append(legacy_search_query)

    # Provider-specific arrays — accept either naming convention.
    for key in ("search_queries", "query_variants"):
        raw = result.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            if isinstance(item, str):
                item = item.strip()
                if item:
                    variants.append(item)

    # Legacy sub_queries already existed; keep them in the same stream.
    raw_subs = result.get("sub_queries", [])
    if isinstance(raw_subs, (list, tuple)):
        for item in raw_subs:
            if isinstance(item, str):
                item = item.strip()
                if item:
                    variants.append(item)

    # Deduplicate while preserving order, then bound the count.
    seen = set()
    deduped = []
    for v in variants:
        folded = v.casefold()
        if folded not in seen:
            seen.add(folded)
            deduped.append(v)
    # v2 deliberately caps the model at three alternatives. Legacy payloads
    # retain the previous configurable limit during the migration window.
    variant_limit = max(int(max_variants), 1)
    if is_v2:
        variant_limit = min(variant_limit, 3)
    if len(deduped) > variant_limit:
        logger.debug(
            f"[bdh-bridge] variants truncated from {len(deduped)} to {variant_limit}"
        )
        deduped = deduped[:variant_limit]

    search_query = deduped[0] if deduped else original_query
    sub_queries = deduped[1:] if len(deduped) > 1 else []

    return {
        "schema_version": 2 if is_v2 else 1,
        "should_retrieve": should_retrieve,
        "store_candidate": store_candidate,
        # Keep the v1 alias in normalized output for callers/tests that have
        # not migrated yet. New code must use should_retrieve explicitly.
        "should_query": should_retrieve,
        "query": original_query,
        "search_query": search_query,
        "sub_queries": sub_queries,
        "knowledge_types": _normalize_knowledge_types(result.get("knowledge_types")),
        "confidence": _normalize_confidence(result.get("confidence")),
    }


def _load_rewrite_system_prompt():
    """Load the rewrite system prompt.

    Returns the prompt from BDH_REWRITE_PROMPT_FILE if it exists and is
    non-empty; otherwise returns the embedded default. This keeps the bridge
    agnostic: a user can adapt the prompt to their language/vault without
    forking the code.
    """
    if _REWRITE_PROMPT_FILE:
        path = Path(_REWRITE_PROMPT_FILE).expanduser()
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError as e:
            logger.warning(f"[bdh-bridge] could not read custom prompt file {path}: {e} — using default")
    return _DEFAULT_REWRITE_SYSTEM_PROMPT


_REWRITE_SYSTEM_PROMPT = _load_rewrite_system_prompt()


def _current_rewrite_api_key():
    """Resolve the rewrite credential at call time, not only at import time.

    The gateway is launched by launchd and plugin discovery/import order can
    precede the final .env load.  Reading the module-level value only once
    makes the bridge permanently believe rewrite is disabled for that process.
    Prefer the dedicated bridge credential, then the shared Ollama credential,
    while retaining the import-time value for backwards compatibility/tests.
    """
    return (
        os.environ.get("BDH_REWRITE_API_KEY", "").strip()
        or os.environ.get("OLLAMA_API_KEY", "").strip()
        or _REWRITE_API_KEY.strip()
    )


def _rewrite_provider_candidates():
    """Return the ordered rewrite providers: Ollama, OpenRouter, then oMLX."""
    return [
        {
            "provider": "ollama-cloud",
            "url": _REWRITE_API_URL,
            "model": _REWRITE_MODEL,
            "api_key": _current_rewrite_api_key(),
        },
        {
            "provider": "openrouter",
            "url": _REWRITE_OPENROUTER_URL,
            "model": _REWRITE_OPENROUTER_MODEL,
            "api_key": os.environ.get("OPENROUTER_API_KEY", "").strip(),
        },
        {
            "provider": "omlx",
            "url": _REWRITE_LOCAL_URL,
            "model": _REWRITE_LOCAL_MODEL,
            "api_key": "",
        },
    ]


def _is_rewrite_provider_local(provider):
    """Return whether a rewrite provider is local and needs no API key."""
    return provider == "omlx"


def _is_rewrite_provider_openai_compatible(provider):
    """Return whether the provider uses the OpenAI-compatible JSON contract."""
    return provider in {"openrouter", "omlx"}


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


def _resolve_vault_id(explicit=None):
    """Resolve an explicit vault, then the bridge default, else BDH default."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    configured = os.environ.get("BDH_VAULT_ID", "").strip()
    return configured or None


def _vault_id_from_hook(kwargs):
    """Read vault scope supplied by Hermes, falling back to bridge config."""
    for key in ("vault_id", "bdh_vault_id"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("vault_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _resolve_vault_id()


def _turn_state_key(kwargs):
    """Return a turn-aware state key, falling back to the session identifier."""
    session_key = _turn_key(kwargs)
    if session_key is None:
        return None
    turn_id = kwargs.get("turn_id")
    if turn_id is not None:
        return str(session_key), str(turn_id)
    return str(session_key)


def _cleanup_turn_states_locked(now):
    """Evict expired state and oldest overflow entries while holding the lock."""
    expired = [
        key for key, state in _turn_states.items()
        if now - state["created_at"] >= _TURN_STATE_TTL_SECONDS
    ]
    for key in expired:
        _turn_states.pop(key, None)

    overflow = len(_turn_states) - _TURN_STATE_MAX_ENTRIES
    if overflow > 0:
        for key in list(_turn_states)[:overflow]:
            _turn_states.pop(key, None)


def _remember_turn_state(kwargs, user_message):
    """Store per-turn write state before pre-hook gating can return early."""
    key = _turn_state_key(kwargs)
    if key is None:
        return
    now = time.time()
    with _bdh_state_lock:
        _cleanup_turn_states_locked(now)
        _turn_states.pop(key, None)
        _turn_states[key] = {
            "created_at": now,
            "user_message": user_message,
            "vault_id": _vault_id_from_hook(kwargs),
            "rewritten_query": "",
            "should_retrieve": None,
            "store_candidate": None,
            # Legacy alias retained for old callers during the v1→v2 rollout.
            "should_query": None,
        }
        _cleanup_turn_states_locked(now)


def _update_turn_state(kwargs, rewritten_query, should_retrieve,
                       store_candidate=None):
    """Attach v2 routing decisions to the state captured for this turn.

    ``store_candidate`` defaults to the v1 coupled behavior when omitted, so
    existing integrations can migrate without changing their call signature.
    """
    key = _turn_state_key(kwargs)
    if key is None:
        return
    if not isinstance(should_retrieve, bool):
        return
    if store_candidate is None:
        store_candidate = should_retrieve
    if not isinstance(store_candidate, bool):
        return
    with _bdh_state_lock:
        state = _turn_states.get(key)
        if state is not None:
            state["rewritten_query"] = rewritten_query
            state["should_retrieve"] = should_retrieve
            state["store_candidate"] = store_candidate
            state["should_query"] = should_retrieve


def _pop_turn_state(kwargs):
    """Return and remove state for a completed turn after pruning stale entries."""
    key = _turn_state_key(kwargs)
    if key is None:
        return None
    with _bdh_state_lock:
        _cleanup_turn_states_locked(time.time())
        return _turn_states.pop(key, None)


# ---------------------------------------------------------------------------
# Session-end synthesis helpers (v0.7.0)
# ---------------------------------------------------------------------------

def _remember_session_turn(session_id, user_message, assistant_text):
    """Append one written turn to the per-session synthesis buffer.

    Only called when the per-turn write path actually succeeded, so the
    synthesis never introduces content that wasn't already fed to BDH.
    """
    if not session_id or not _SESSION_SYNTH_ENABLED:
        return
    session_id = str(session_id)
    with _bdh_state_lock:
        # A late callback from a write submitted after the lifecycle boundary
        # must not resurrect a session that was already drained.
        if session_id in _flushed_sessions:
            return
        buf = _session_buffers.setdefault(session_id, [])
        # Cap buffer length to bound memory on very long sessions.
        if len(buf) >= 200:
            buf.pop(0)
        buf.append({
            "user": (user_message or "")[:1500],
            "assistant": (assistant_text or "")[:1500],
        })


def _flush_session_synthesis(session_id):
    """Fire-and-forget a curated session synthesis to BDH, if worth it.

    If per-turn writes are still in flight, mark the lifecycle boundary and
    let their completion callback retry the flush. The hook itself remains
    non-blocking and failed writes never enter the transcript buffer.
    """
    if not _SESSION_SYNTH_ENABLED or not session_id:
        return
    session_id = str(session_id)
    with _bdh_state_lock:
        if session_id in _flushed_sessions:
            return
        if _session_pending_writes.get(session_id, 0):
            _session_finalize_requested.add(session_id)
            return
        _flushed_sessions.add(session_id)
        _session_finalize_requested.discard(session_id)
        buf = _session_buffers.pop(session_id, None)
    if not buf:
        return
    if len(buf) < _SESSION_SYNTH_MIN_TURNS:
        logger.debug(
            f"[bdh-bridge] session {session_id}: only {len(buf)} turn(s), "
            f"below min {_SESSION_SYNTH_MIN_TURNS} — skipping synthesis"
        )
        return

    # Compact the transcript, keeping user questions and assistant answers.
    lines = []
    for t in buf:
        u = t.get("user", "").strip()
        a = t.get("assistant", "").strip()
        if not u:
            continue
        lines.append(f"USER: {u}")
        if a:
            lines.append(f"ASSISTANT: {a}")
    transcript = "\n".join(lines)
    if len(transcript) > _SESSION_SYNTH_MAX_CHARS:
        transcript = transcript[-_SESSION_SYNTH_MAX_CHARS:]

    query = (
        "Synthesis of an entire agent session. Extract and record any durable "
        "concept, decision, architecture choice, lesson learned, or reusable "
        "insight that emerged across the conversation as a whole — not per-turn "
        "noise. Ignore operational status, diagnostics, and transient tasks."
    )
    # _bdh_query_async already spawns its own daemon thread (fire-and-forget),
    # so call it directly — no nested thread needed.
    try:
        _bdh_query_async(
            query_text=query,
            user_prompt=transcript,
            source="session_synthesis",
        )
        logger.info(f"[bdh-bridge] session {session_id} synthesis queued ({len(buf)} turns)")
    except Exception as e:
        logger.warning(f"[bdh-bridge] session synthesis start error: {e}")


def _session_write_complete(session_id):
    """Release one pending write and flush if lifecycle already finalized it."""
    if not session_id:
        return
    session_id = str(session_id)
    should_flush = False
    with _bdh_state_lock:
        pending = _session_pending_writes.get(session_id, 0)
        if pending <= 1:
            _session_pending_writes.pop(session_id, None)
            should_flush = session_id in _session_finalize_requested
        else:
            _session_pending_writes[session_id] = pending - 1
    if should_flush:
        _flush_session_synthesis(session_id)


def _on_session_finalize(**kwargs):
    """Flush synthesis buffer for a session identity being torn down.

    Hermes fires on_session_finalize when the CLI or gateway tears down an
    active session identity. The flush is deferred until any in-flight turn
    writes have completed.
    """
    try:
        session_id = kwargs.get("session_id")
        if session_id:
            _flush_session_synthesis(session_id)
    except Exception as e:
        logger.debug(f"[bdh-bridge] on_session_finalize error: {e}")


def _on_session_reset(**kwargs):
    """Flush synthesis for the old identity when Hermes replaces a session."""
    try:
        old_session_id = kwargs.get("old_session_id")
        if old_session_id:
            _flush_session_synthesis(old_session_id)
    except Exception as e:
        logger.debug(f"[bdh-bridge] on_session_reset error: {e}")


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
    url = f"{_current_bdh_api()}{endpoint}"
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
                    learn=True, retries=2, vault_id=None, query_variants=None):
    """Synchronous query to BDH.

    ``learn=False`` is used by automatic pre-LLM retrieval: it must provide
    context without changing Hebbian state or running neurogenesis.
    """
    payload = {
        "query": query_text,
        "learn": learn,
        "respond": not (source == "automatic_retrieval" and not learn),
    }
    vault_id = _resolve_vault_id(vault_id)
    if vault_id:
        payload["vault_id"] = vault_id
    if user_prompt:
        payload["user_prompt"] = user_prompt
    if source:
        payload["source"] = source
    if query_variants:
        payload["query_variants"] = query_variants
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

    variants = (result.get("routing") or {}).get("query_variants") or []
    rendered_variants = 0
    for variant in variants:
        if rendered_variants >= 3 or not isinstance(variant, dict):
            continue
        query = variant.get("query")
        if not isinstance(query, str) or not query.strip():
            continue
        if rendered_variants == 0:
            lines.extend(["", "Query variants (retrieval only):"])
        language = str(variant.get("language") or "unknown").replace("\n", " ")[:24]
        query = " ".join(query.split())[:240]
        lines.append(f"- [{language}] {query}")
        rendered_variants += 1
    if synthesis:
        lines.extend(["", "Relevant graph synthesis:", synthesis[:4000]])
    lines.extend([
        "", "Use this as supporting context.",
        "Do not mention BDH unless relevant.",
        "If it conflicts with the current conversation, prefer the current conversation.",
        "[/BDH CONTEXT]",
    ])
    return "\n".join(lines)


def _bdh_query_async(query_text, user_prompt=None, source="assistant_response",
                     on_success=None, on_complete=None, vault_id=None):
    """Fire-and-forget query — used by hooks.

    Short timeout (30s) and 1 retry. If BDH is down, the daemon thread
    exits quickly instead of piling up.

    Args:
        on_success: Optional zero-arg callback invoked **only** after the
            HTTP request succeeds (result is not None). Called from the
            daemon thread and must be thread-safe.
        on_complete: Optional zero-arg callback invoked exactly once after
            the worker has a result, including failure. This lets lifecycle
            code release pending-write state without waiting or joining.
    """
    def _worker():
        payload = {"query": query_text}
        resolved_vault_id = _resolve_vault_id(vault_id)
        if resolved_vault_id:
            payload["vault_id"] = resolved_vault_id
        if user_prompt:
            payload["user_prompt"] = user_prompt
        if source:
            payload["source"] = source

        result = None
        try:
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
                if on_success is not None:
                    try:
                        on_success()
                    except Exception as cb_err:
                        logger.warning(
                            f"[bdh-bridge] on_success callback error: {cb_err}"
                        )
            else:
                logger.warning("[bdh-bridge] BDH unreachable — consolidation or server down")
        finally:
            if on_complete is not None:
                try:
                    on_complete()
                except Exception as cb_err:
                    logger.warning(
                        f"[bdh-bridge] on_complete callback error: {cb_err}"
                    )

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Query rewrite pipeline (v0.8.0)
# ---------------------------------------------------------------------------

def _extract_context(conversation_history, n=_CONTEXT_MESSAGES_N,
                     max_chars=_CONTEXT_MSG_MAX_CHARS, exclude_message=""):
    """Extract compact conversational context, excluding the current turn.

    Hermes passes conversation_history in pre_llm_call kwargs. Only user and
    assistant messages are trusted as rewrite evidence; system/tool content is
    deliberately excluded because it can contain instructions or large payloads.
    """
    if not isinstance(conversation_history, list) or not conversation_history:
        return ""

    # Filter before taking the tail so tool/system entries cannot consume the
    # context budget or instruct the rewrite model.
    conversational = [
        msg for msg in conversation_history
        if isinstance(msg, dict)
        and msg.get("role") in {"user", "assistant"}
    ]
    lines = []
    for msg in conversational[-n:]:
        role = msg.get("role")
        content = msg.get("content", "")
        # Handle Anthropic block format
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        elif not isinstance(content, str):
            content = str(content)
        if not content.strip() or (
            exclude_message and content.strip() == exclude_message.strip()
        ):
            continue
        # Truncate long messages
        snippet = content[:max_chars]
        lines.append(f"[{role}] {snippet}")

    return "\n".join(lines)


def _merge_search_query(primary, sub_queries):
    """Build a single BDH query from primary search_query + sub-queries.

    We pass sub-queries to BDH separated by newlines so the vector/BM25
    index can rank all candidates together. BDH treats each line as an
    alternative phrasing, not as a boolean AND.
    """
    parts = [primary.strip()]
    parts.extend(sq.strip() for sq in sub_queries)
    return "\n".join(p for p in parts if p) or primary


def _rewrite_query(user_message, context_text=""):
    """Call the rewrite LLM to classify + rewrite the user message.

    Returns a normalized dict with:
      - should_retrieve: bool (whether BDH should be queried)
      - store_candidate: bool (whether the turn may enter the write path)
      - query: str (canonical query in user's language, or original if fallback)
      - search_query: str (optimized English/keyword query for BDH retrieval)
      - sub_queries: list[str] (additional search queries if multi-topic)

    The raw LLM output is normalized through `_normalize_query_variants` so
    provider-specific fields (`search_queries`, `query_variants`) are accepted
    alongside the legacy `search_query`/`sub_queries` fields. The original
    `query` is preserved unchanged for the write path.

    On any failure (timeout, parse error, network, or malformed normalized
    output), returns None so the caller falls back to the mechanical gate + raw
    user message.
    """
    user_content = f"User message:\n{user_message[:1500]}"
    if context_text:
        user_content += f"\n\nRecent context:\n{context_text}"

    # Reload prompt each call so external edits take effect without restart.
    system_prompt = _load_rewrite_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    for candidate in _rewrite_provider_candidates():
        provider = candidate["provider"]
        api_key = candidate["api_key"]
        if not api_key and not _is_rewrite_provider_local(provider):
            logger.debug(
                f"[bdh-bridge] rewrite provider skipped — missing API key: {provider}"
            )
            continue

        payload = {
            "model": candidate["model"],
            "messages": messages,
            "temperature": 0.1,
            "stream": False,
            # Qwen/oMLX must not emit hidden reasoning around the JSON contract.
            "chat_template_kwargs": {
                "enable_thinking": False,
                "thinking": False,
            },
        }
        if _is_rewrite_provider_openai_compatible(provider):
            payload["response_format"] = {"type": "json_object"}
        else:
            # Ollama-compatible cloud endpoints accept this provider-specific
            # JSON response hint; OpenAI-compatible local endpoints use the
            # standard response_format above.
            payload["format"] = "json"

        url = f"{candidate['url']}/chat/completions"
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = _REWRITE_HTTP_REFERER or "https://github.com/bdh-graph-harness"
            headers["X-Title"] = _REWRITE_APP_TITLE
        elif _REWRITE_APP_TITLE:
            headers["X-Title"] = _REWRITE_APP_TITLE

        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=_REWRITE_TIMEOUT) as resp:
                data = json.loads(resp.read())

            # OpenAI-compatible response format
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("rewrite provider returned empty content")

            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            raw_result = json.loads(content)

            # Normalize optional provider-specific variant fields. If the result
            # is malformed, fail over instead of disabling rewrite immediately.
            result = _normalize_query_variants(
                raw_result, user_message, max_variants=_REWRITE_MAX_VARIANTS
            )
            if result is None:
                raise ValueError("rewrite output normalization failed")

            logger.info(
                f"[bdh-bridge] rewrite provider={provider}: "
                f"should_retrieve={result.get('should_retrieve', result.get('should_query'))}, "
                f"store_candidate={result.get('store_candidate', result.get('should_query'))}, "
                f"query={result['query'][:80]!r}, search_query={result['search_query'][:80]!r}, "
                f"sub_queries={len(result['sub_queries'])}"
            )
            return result

        except (URLError, OSError, ValueError, json.JSONDecodeError,
                KeyError, IndexError, TypeError, AttributeError) as e:
            logger.warning(
                f"[bdh-bridge] rewrite provider failed: {provider} — {e}; trying next"
            )
            continue

    logger.warning(
        "[bdh-bridge] all rewrite providers failed — fallback to mechanical gate + raw"
    )
    return None




# ---------------------------------------------------------------------------
# Hook: pre_llm_call — capture user message for write path context
# ---------------------------------------------------------------------------

def _on_pre_llm_call(**kwargs):
    """Capture the user message and optionally retrieve read-only BDH context.

    v0.8.0: when BDH_QUERY_REWRITE_ENABLED is true, the hook first calls an
    LLM to route and rewrite the user message. Retrieval and storage decisions
    are independent. On any LLM failure, it falls back to the mechanical gate
    + raw user message.
    """
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
        _remember_turn_state(kwargs, msg)

        if _is_cron_source(kwargs.get("platform"), kwargs.get("source")) and not _cron_has_bdh_opt_in(msg):
            logger.info("[bdh-bridge] automatic retrieval skipped — cron source is deny-by-default")
            return None

        if _is_prompt_blacklisted(msg):
            logger.info("[bdh-bridge] automatic retrieval skipped — prompt is blacklisted")
            return None

        # ── Query rewrite pipeline ──────────────────────────────────────
        bdh_search_query = msg[:1500]
        bdh_query_variants = None
        should_retrieve = None
        store_candidate = None
        if _QUERY_REWRITE_ENABLED:
            # Extract context from conversation_history
            context_text = _extract_context(
                kwargs.get("conversation_history"), exclude_message=msg
            )

            # Call the rewrite LLM (classify + rewrite in one shot)
            rewrite_result = _rewrite_query(msg, context_text)

            if rewrite_result is not None:
                should_retrieve = rewrite_result.get(
                    "should_retrieve", rewrite_result.get("should_query")
                )
                store_candidate = rewrite_result.get(
                    "store_candidate", should_retrieve
                )
                # A monkeypatched/third-party rewrite adapter is still held to
                # the same strict contract as the built-in parser.
                if (
                    not isinstance(should_retrieve, bool)
                    or not isinstance(store_candidate, bool)
                ):
                    logger.warning(
                        "[bdh-bridge] rewrite returned invalid routing booleans — fallback to raw"
                    )
                    rewrite_result = None
                else:
                    _update_turn_state(
                        kwargs,
                        rewrite_result.get("query", ""),
                        should_retrieve,
                        store_candidate,
                    )

            if rewrite_result is not None:
                if not should_retrieve:
                    logger.info(
                        "[bdh-bridge] rewrite: retrieval disabled; "
                        f"store_candidate={store_candidate}"
                    )
                    return None

                # Keep the rewritten user-language query as the canonical seed.
                # Send translated/sub-query outputs as independent structured
                # variants so BDH can fuse results instead of receiving keyword soup.
                bdh_search_query = rewrite_result["query"][:1500]
                bdh_query_variants = []
                if rewrite_result.get("search_query"):
                    bdh_query_variants.append({
                        "query": rewrite_result["search_query"][:1500],
                        "language": "rewrite",
                        "weight": 1.0,
                    })
                bdh_query_variants.extend(
                    {"query": variant[:1500], "language": "rewrite", "weight": 1.0}
                    for variant in rewrite_result.get("sub_queries", [])
                    if isinstance(variant, str) and variant.strip()
                )
                if not bdh_query_variants:
                    bdh_query_variants = None
            else:
                # Fallback: LLM failed, use mechanical gate + raw message
                if not _should_auto_retrieve(msg):
                    return None
        else:
            # Feature flag off: use mechanical gate + raw message (v0.4.0 behavior)
            if not _should_auto_retrieve(msg):
                return None

        # ── BDH retrieval (read-only) ──────────────────────────────────
        result = _bdh_query_sync(
            bdh_search_query,
            source="automatic_retrieval",
            timeout=2,
            learn=False,
            retries=1,
            vault_id=_vault_id_from_hook(kwargs),
            query_variants=bdh_query_variants,
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
    """Asynchronously feed the user message + assistant response to BDH.

    The write path uses the original rewritten query (user-language) as the
    embedding seed, not the optimized search_query, so the vault learns from
    real user language rather than translated/keyword artifacts.
    """
    try:
        # Consume state before any return path so missing/aborted responses do
        # not leave cross-turn state behind.
        turn_state = _pop_turn_state(kwargs)
        finish_reason = kwargs.get("finish_reason", "")
        if finish_reason != "stop":
            return

        assistant_msg = kwargs.get("assistant_message")
        if assistant_msg is None:
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
        if not turn_state or not turn_state["user_message"].strip():
            logger.debug("[bdh-bridge] skipping write — no user message captured")
            return
        user_message = turn_state["user_message"]

        if _is_prompt_blacklisted(user_message):
            logger.info("[bdh-bridge] write skipped — prompt is blacklisted")
            return

        # v0.6.0: cron deny-by-default is enforced at both read and write. Without
        # an explicit opt-in, the write path must be skipped regardless of any
        # previous turn state. This protects the vault from scheduled-agent noise.
        if _is_cron_source(kwargs.get("platform"), kwargs.get("source")) and not _cron_has_bdh_opt_in(user_message):
            logger.info("[bdh-bridge] write skipped — cron source is deny-by-default")
            return

        # v0.8.0: retrieval and storage are independent decisions. The legacy
        # alias is checked only for callers that populated old turn state.
        store_candidate = turn_state.get("store_candidate")
        if store_candidate is None:
            store_candidate = turn_state.get("should_query")
        if store_candidate is False:
            logger.info("[bdh-bridge] write skipped — store_candidate=false")
            return

        # Use the USER MESSAGE as the embedding seed (query) — that's the signal.
        # The assistant response is passed as user_prompt for LLM/neurogenesis context.
        # v0.5.0: prefer the rewritten query for read/write consistency.
        # v0.6.0: _normalize_query_variants guarantees `query` is always the
        # user-language/intent field, so the write path stays provider-neutral.
        query = (turn_state["rewritten_query"] or user_message)[:1500]
        user_prompt = text[:1500]

        # v0.7.0/#15: remember the turn only after a successful write, and
        # release the pending barrier on both success and failure.
        session_id = kwargs.get("session_id")
        pending_registered = bool(session_id and _SESSION_SYNTH_ENABLED)
        if pending_registered:
            with _bdh_state_lock:
                session_id = str(session_id)
                _session_pending_writes[session_id] = (
                    _session_pending_writes.get(session_id, 0) + 1
                )

        try:
            _bdh_query_async(
                query, user_prompt=user_prompt, source="assistant_response",
                vault_id=turn_state.get("vault_id"),
                on_success=lambda: _remember_session_turn(
                    session_id, user_message, text,
                ),
                on_complete=(
                    lambda: _session_write_complete(session_id)
                    if pending_registered else None
                ),
            )
        except Exception:
            if pending_registered:
                _session_write_complete(session_id)
            raise

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
# Tool: bdh_stats — graph statistics
# ---------------------------------------------------------------------------

def _tool_bdh_stats(args, **kwargs):
    """Return BDH graph statistics for a vault.

    Args:
        vault_id: Optional vault identifier. Uses BDH default if omitted.

    Returns:
        JSON with neuron_count, active_count, dormant_count, synapse_count,
        hebbian_count, average_degree.
    """
    try:
        vault_id = args.get("vault_id", "")
        endpoint = "/api/stats"
        if vault_id:
            endpoint += f"?{urllib.parse.urlencode({'vault_id': vault_id}, quote_via=urllib.parse.quote)}"

        result = _bdh_request(endpoint, timeout=10, retries=2)
        if result is None:
            return json.dumps({
                "error": "BDH server unreachable — possibly in consolidation."
            })

        output = {
            "neuron_count": result.get("neuron_count", 0),
            "active_count": result.get("active_count", 0),
            "dormant_count": result.get("dormant_count", 0),
            "synapse_count": result.get("synapse_count", 0),
            "hebbian_count": result.get("hebbian_count", 0),
            "average_degree": result.get("average_degree", 0.0),
        }
        return json.dumps(output, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"bdh_stats tool error: {e}"})


# ---------------------------------------------------------------------------
# Hermes plugin entry point
# ---------------------------------------------------------------------------

_BDH_QUERY_SCHEMA = {
    "name": "bdh_query",
    "description": "Query the BDH knowledge graph for relevant context.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Question or topic to search in the BDH graph.",
            },
            "vault_id": {
                "type": "string",
                "description": "Optional BDH vault identifier.",
            },
        },
        "required": ["query"],
    },
}

_BDH_STATS_SCHEMA = {
    "name": "bdh_stats",
    "description": "Return current BDH graph statistics for a vault.",
    "parameters": {
        "type": "object",
        "properties": {
            "vault_id": {
                "type": "string",
                "description": "Optional BDH vault identifier.",
            },
        },
        "required": [],
    },
}

# Minimal plugin metadata returned for Hermes introspection.
PLUGIN = {
    "name": "bdh-bridge",
    "version": "0.8.0",
    "description": "Bidirectional Hermes ↔ BDH Graph Harness bridge with independent rewrite routing, normalization, session synthesis, and lifecycle hooks.",
}


def register(app):
    """Register hooks and tools with Hermes.

    Args:
        app: Hermes plugin registrar exposing register_hook and register_tool.
    """
    app.register_hook("pre_llm_call", _on_pre_llm_call)
    app.register_hook("post_api_request", _on_post_api_request)
    app.register_hook("post_tool_call", _on_post_tool_call)
    app.register_hook("transform_llm_output", _on_transform_llm_output)
    # v0.7.1: session lifecycle hooks for synthesis flush (issue #14)
    app.register_hook("on_session_finalize", _on_session_finalize)
    app.register_hook("on_session_reset", _on_session_reset)

    app.register_tool(
        name="bdh_query",
        toolset="bdh",
        schema=_BDH_QUERY_SCHEMA,
        handler=_tool_bdh_query,
        description="Query the BDH knowledge graph for context.",
    )
    app.register_tool(
        name="bdh_stats",
        toolset="bdh",
        schema=_BDH_STATS_SCHEMA,
        handler=_tool_bdh_stats,
        description="Return BDH graph statistics for a vault.",
    )
