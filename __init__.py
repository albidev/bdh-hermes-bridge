"""
BDH Bridge — Bidirectional Hermes ↔ BDH Graph Harness integration.

Write path: feeds session content to BDH after each API response.
Read path: provides bdh_query and bdh_stats tools.

v0.2.0:
  - Echo-loop dampening: assistant responses flagged as source="assistant_response"
  - User context capture: pre_llm_call hook stores last user message for write path
  - Retry with exponential backoff on BDH requests
"""

import json
import logging
import threading
import time
import urllib.request
from urllib.error import URLError

logger = logging.getLogger("bdh-bridge")

BDH_API = "http://localhost:8643"
_write_queue = []
_queue_lock = threading.Lock()

# ---------------------------------------------------------------------------
# State: capture user message from pre_llm_call for write path
# ---------------------------------------------------------------------------

_last_user_message = ""


# ---------------------------------------------------------------------------
# BDH HTTP helpers — with retry + exponential backoff
# ---------------------------------------------------------------------------

def _bdh_request(endpoint, data=None, timeout=10, retries=3, backoff_base=2.0):
    """HTTP request to BDH API with retry + exponential backoff.

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
            if attempt < retries - 1:
                wait = backoff_base ** attempt  # 1s, 2s, 4s
                logger.warning(
                    f"[bdh-bridge] request failed (attempt {attempt + 1}/{retries}): {e} "
                    f"— retrying in {wait}s"
                )
                time.sleep(wait)

    logger.error(f"[bdh-bridge] all {retries} retries exhausted for {endpoint}: {last_error}")
    return None


def _bdh_query_sync(query_text, user_prompt=None, source=None, timeout=15):
    """Synchronous query to BDH — used by bdh_query tool."""
    payload = {"query": query_text}
    if user_prompt:
        payload["user_prompt"] = user_prompt
    if source:
        payload["source"] = source
    return _bdh_request("/api/query", payload, timeout=timeout)


def _bdh_query_async(query_text, user_prompt=None, source="assistant_response"):
    """Fire-and-forget query — used by hooks."""
    def _worker():
        payload = {"query": query_text}
        if user_prompt:
            payload["user_prompt"] = user_prompt
        if source:
            payload["source"] = source

        result = _bdh_request("/api/query", payload, timeout=20)
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
    msg = kwargs.get("user_message", "")
    if msg and isinstance(msg, str):
        _last_user_message = msg


# ---------------------------------------------------------------------------
# Hook: post_api_request — feed BDH after each final response
# ---------------------------------------------------------------------------

def _on_post_api_request(**kwargs):
    """After the LLM finishes a response, query BDH with the assistant content.

    Only fires on final responses (finish_reason == "stop") and only when
    the response is substantial enough to be worth querying.

    Sends source="assistant_response" so BDH can dampen Hebbian learning
    and prevent echo-loop reinforcement.
    """
    finish_reason = kwargs.get("finish_reason", "")
    if finish_reason != "stop":
        return

    content_chars = kwargs.get("assistant_content_chars", 0) or 0
    if content_chars < 200:
        return  # skip trivial responses ("done", "ok", "pushato")

    assistant_msg = kwargs.get("assistant_message")
    if not assistant_msg:
        return

    text = getattr(assistant_msg, "content", "") or ""
    if not text.strip():
        return

    # Truncate to avoid sending massive responses to BDH
    query = text[:1500]

    # Include user context for proper question→answer associations
    user_prompt = _last_user_message[:1500] if _last_user_message else None

    _bdh_query_async(query, user_prompt=user_prompt, source="assistant_response")


# ---------------------------------------------------------------------------
# Tool: bdh_query — query BDH graph for context
# ---------------------------------------------------------------------------

def _tool_bdh_query(args):
    """Query the BDH knowledge graph.

    Args:
        query: The question or topic to search for in the knowledge graph.

    Returns:
        JSON with activated_notes, response, new_concepts, hebbian_updates.
    """
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


# ---------------------------------------------------------------------------
# Tool: bdh_stats — quick graph stats
# ---------------------------------------------------------------------------

def _tool_bdh_stats(args):
    """Get current BDH graph statistics.

    Returns:
        JSON with neuron count, active/dormant, synapses, hebbian, avg_degree.
    """
    result = _bdh_request("/api/stats", timeout=5)
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
        _tool_bdh_query,
        description="Query the BDH knowledge graph. Returns activated neurons, "
                    "LLM response, and any new concepts created via neurogenesis. "
                    "Use when you need context from the BDH graph about a topic."
    )
    ctx.register_tool(
        "bdh_stats",
        _tool_bdh_stats,
        description="Get current BDH graph statistics: neuron count, active/dormant, "
                    "synapses, hebbian links, average degree."
    )

    logger.info(
        "[bdh-bridge] registered: hooks=[pre_llm_call, post_api_request], "
        "tools=[bdh_query, bdh_stats]"
    )
