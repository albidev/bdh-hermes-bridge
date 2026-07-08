"""
BDH Bridge — Bidirectional Hermes ↔ BDH Graph Harness integration.

Write path: feeds session content to BDH after each API response.
Read path: provides bdh_query and bdh_stats tools.
"""

import json
import logging
import threading
import urllib.request
from urllib.error import URLError

logger = logging.getLogger("bdh-bridge")

BDH_API = "http://localhost:8643"
_write_queue = []
_queue_lock = threading.Lock()


# ---------------------------------------------------------------------------
# BDH HTTP helpers
# ---------------------------------------------------------------------------

def _bdh_request(endpoint, data=None, timeout=10):
    """Fire-and-forget HTTP request to BDH API."""
    try:
        url = f"{BDH_API}{endpoint}"
        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body,
                                        headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (URLError, OSError, json.JSONDecodeError) as e:
        logger.warning(f"BDH request failed: {e}")
        return None


def _bdh_query_sync(query_text, timeout=15):
    """Synchronous query to BDH — used by bdh_query tool."""
    return _bdh_request("/api/query", {"query": query_text}, timeout=timeout)


def _bdh_query_async(query_text):
    """Fire-and-forget query — used by hooks."""
    def _worker():
        result = _bdh_request("/api/query", {"query": query_text}, timeout=20)
        if result:
            new = result.get("new_concepts", [])
            activated = len(result.get("activated_notes", []))
            hebbian = len(result.get("hebbian_updates", []))
            if new:
                logger.info(f"[bdh-bridge] neurogenesis: {len(new)} new concepts "
                            f"({activated} activated, {hebbian} hebbian updates)")
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Hook: post_api_request — feed BDH after each final response
# ---------------------------------------------------------------------------

def _on_post_api_request(**kwargs):
    """After the LLM finishes a response, query BDH with the assistant content.

    Only fires on final responses (finish_reason == "stop") and only when
    the response is substantial enough to be worth querying.
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
    _bdh_query_async(query)


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

    result = _bdh_query_sync(query)
    if result is None:
        return json.dumps({"error": "BDH server unreachable"})

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
        JSON with neuron count, synapses, hebbian, dormant/active, avg_degree.
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

    logger.info("[bdh-bridge] registered: hook=post_api_request, tools=[bdh_query, bdh_stats]")
