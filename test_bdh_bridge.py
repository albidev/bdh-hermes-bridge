"""Tests for the BDH bridge read/write turn contract."""

import importlib.util
import json
import threading
import time
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "bdh_bridge", Path(__file__).with_name("__init__.py")
)
bridge = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(bridge)


def test_gating_skips_casual_messages():
    assert bridge._should_auto_retrieve("ciao") is False
    assert bridge._should_auto_retrieve("Grazie!") is False
    assert bridge._should_auto_retrieve("ok, perfetto") is False


def test_prompt_blacklist_matches_literal_substrings(tmp_path, monkeypatch):
    blacklist = tmp_path / "blacklist.txt"
    blacklist.write_text(
        "# comment\nReview the conversation above and update the skill library.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "_PROMPT_BLACKLIST_FILE", blacklist)
    assert bridge._is_prompt_blacklisted(
        "Review the conversation above and update the skill library. Be ACTIVE."
    ) is True
    assert bridge._is_prompt_blacklisted("Explain the BDH routing gate") is False


def test_blacklisted_prompt_skips_automatic_retrieval(monkeypatch, tmp_path):
    blacklist = tmp_path / "blacklist.txt"
    blacklist.write_text("skill library\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_PROMPT_BLACKLIST_FILE", blacklist)
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected BDH query")),
    )
    assert bridge._on_pre_llm_call(
        session_id="blacklisted-session",
        user_message="Review the conversation above and update the skill library.",
    ) is None


def test_blacklisted_prompt_skips_write(monkeypatch, tmp_path):
    blacklist = tmp_path / "blacklist.txt"
    blacklist.write_text("skill library\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_PROMPT_BLACKLIST_FILE", blacklist)
    monkeypatch.setattr(bridge, "_bdh_query_async", lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("unexpected BDH write"))
    ))
    state_kwargs = {"session_id": "blacklisted-session"}
    bridge._remember_turn_state(
        state_kwargs, "Review the conversation above and update the skill library."
    )
    bridge._on_post_api_request(
        session_id="blacklisted-session",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )


def test_cron_skips_automatic_retrieval_by_default(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cron queried BDH")),
    )
    assert bridge._on_pre_llm_call(
        session_id="cron-session",
        platform="cron",
        user_message="Review the latest project architecture and explain the changes.",
    ) is None


def test_cron_skips_write_by_default(monkeypatch):
    writes = []
    monkeypatch.setattr(
        bridge,
        "_bdh_query_async",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    state_kwargs = {"session_id": "cron-session"}
    bridge._remember_turn_state(
        state_kwargs, "Review the latest project architecture and explain the changes."
    )
    bridge._on_post_api_request(
        session_id="cron-session",
        platform="cron",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
    assert writes == []


def test_cron_bdh_opt_in_allows_read_and_write(monkeypatch):
    # This test exercises the legacy cron opt-in path; keep the rewrite LLM
    # disabled so it remains deterministic regardless of the host environment.
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", False)
    calls = []

    def fake_sync(query, **kwargs):
        calls.append(("read", query, kwargs))
        return {
            "routing": {
                "hybrid_top_score": 0.8,
                "vector_top_score": 0.8,
                "bm25_matched_term_count": 2,
            },
            "activated_notes": [{"id": "n1", "title": "Cron BDH", "score": 0.91}],
            "response": "Relevant context.",
        }

    def fake_async(query, **kwargs):
        calls.append(("write", query, kwargs))

    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_sync)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)
    prompt = f"{bridge.BDH_CRON_OPT_IN_MARKER} Explain the scheduled BDH ingestion status."
    result = bridge._on_pre_llm_call(
        session_id="cron-session",
        platform="cron",
        user_message=prompt,
    )
    assert result and "context" in result

    bridge._on_post_api_request(
        session_id="cron-session",
        platform="cron",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
    assert [kind for kind, *_ in calls] == ["read", "write"]


def test_gating_accepts_technical_and_episodic_messages():
    assert bridge._should_auto_retrieve("Come avevamo risolto quel bug del gateway?") is True
    assert bridge._should_auto_retrieve("Perché il plugin BDH va in timeout?") is True
    assert bridge._should_auto_retrieve("Ricordi dove avevamo deciso di mettere la vault episodica?") is True
    assert bridge._should_auto_retrieve("Cos'è l'apprendimento Hebbiano?") is True


def test_pre_llm_returns_ephemeral_context_for_eligible_message(monkeypatch):
    calls = []

    def fake_query(query, **kwargs):
        calls.append((query, kwargs))
        return {
            "activated_notes": [{"id": "n1", "title": "Gateway recovery", "score": 0.91}],
            "response": "The gateway recovery used SQLite recovery.",
        }

    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda msg, ctx="": {
            "should_query": True,
            "query": msg,
            "search_query": "gateway recovery",
            "sub_queries": ["gateway bug resolution"],
        },
    )
    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_query)
    result = bridge._on_pre_llm_call(
        session_id="session-1",
        user_message="Come avevamo risolto quel bug del gateway?",
    )

    assert calls[0][1]["source"] == "automatic_retrieval"
    assert calls[0][1]["timeout"] == 2
    assert calls[0][1]["learn"] is False
    assert calls[0][1]["retries"] == 1
    assert isinstance(calls[0][1]["query_variants"], list)
    assert calls[0][1]["query_variants"]
    assert result and "context" in result
    assert "[BDH CONTEXT — optional]" in result["context"]
    assert "Gateway recovery" in result["context"]
    assert "Use this as supporting context." in result["context"]


def test_context_exposes_capped_query_variants_as_retrieval_only():
    context = bridge._format_bdh_context({
        "activated_notes": [{"id": "n1", "title": "Gateway recovery", "score": 0.91}],
        "routing": {
            "query_variants": [
                {"query": "recupero gateway", "language": "it"},
                {"query": "gateway recovery", "language": "en"},
                {"query": "database recovery path", "language": "rewrite"},
                {"query": "must not be shown", "language": "rewrite"},
            ],
        },
    })

    assert "Query variants (retrieval only):" in context
    assert "- [it] recupero gateway" in context
    assert "- [en] gateway recovery" in context
    assert "- [rewrite] database recovery path" in context
    assert "must not be shown" not in context


def test_hybrid_routing_requires_lexical_or_strong_vector_signal():
    assert bridge._has_relevant_bdh_context({
        "routing": {
            "hybrid_top_score": 0.35,
            "vector_top_score": 0.22,
            "bm25_matched_term_count": 1,
        }
    }) is False
    assert bridge._has_relevant_bdh_context({
        "routing": {
            "hybrid_top_score": 0.35,
            "vector_top_score": 0.22,
            "bm25_matched_term_count": 2,
        }
    }) is True
    assert bridge._has_relevant_bdh_context({
        "routing": {
            "hybrid_top_score": 0.35,
            "vector_top_score": 0.54,
            "bm25_matched_term_count": 0,
        }
    }) is True
    assert bridge._has_relevant_bdh_context({
        "routing": {
            "hybrid_top_score": 0.42,
            "vector_top_score": 0.23,
            "bm25_matched_term_count": 0,
        }
    }) is False


def test_pre_llm_returns_no_context_below_hybrid_score_threshold(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: {
            "activated_notes": [{"id": "weak", "title": "Weak match", "score": 0.12}],
            "response": "",
        },
    )
    result = bridge._on_pre_llm_call(
        session_id="session-low-score",
        user_message="Spiegami un concetto completamente nuovo e non presente nel vault.",
    )
    assert result is None


def test_pre_llm_skips_bdh_for_casual_message(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected BDH query")),
    )
    assert bridge._on_pre_llm_call(session_id="session-2", user_message="Ciao") is None


def test_pre_llm_falls_back_when_bdh_is_offline(monkeypatch):
    monkeypatch.setattr(bridge, "_bdh_query_sync", lambda *args, **kwargs: None)
    result = bridge._on_pre_llm_call(
        session_id="session-3",
        user_message="Perché il plugin BDH va in timeout?",
    )
    assert result is None


def test_sync_query_marks_automatic_retrieval_read_only(monkeypatch):
    captured = {}

    def fake_request(endpoint, data, **kwargs):
        captured.update(endpoint=endpoint, data=data, kwargs=kwargs)
        return {"response": "ok"}

    monkeypatch.setattr(bridge, "_bdh_request", fake_request)
    assert bridge._bdh_query_sync(
        "technical query", source="automatic_retrieval", learn=False, timeout=2, retries=1
    ) == {"response": "ok"}
    assert captured["data"]["learn"] is False
    assert captured["data"]["respond"] is False
    assert captured["data"]["source"] == "automatic_retrieval"


def test_sync_query_omits_vault_when_not_selected(monkeypatch):
    captured = {}

    def fake_request(endpoint, data, **kwargs):
        captured.update(endpoint=endpoint, data=data)
        return {"response": "ok"}

    monkeypatch.setattr(bridge, "_bdh_request", fake_request)
    bridge._bdh_query_sync("query", source="hermes_tool")
    assert "vault_id" not in captured["data"]


def test_tool_query_passes_explicit_vault(monkeypatch):
    captured = {}

    def fake_query(query, **kwargs):
        captured.update(query=query, kwargs=kwargs)
        return {"activated_notes": [], "response": "ok"}

    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_query)
    result = bridge._tool_bdh_query({"query": "research question", "vault_id": "research"})
    assert '"response": "ok"' in result
    assert captured["kwargs"]["vault_id"] == "research"


def test_register_uses_current_plugin_tool_contract():
    calls = []

    class FakeApp:
        def register_hook(self, name, fn):
            pass

        def register_tool(self, **kwargs):
            calls.append(kwargs)

    bridge.register(FakeApp())
    assert [call["name"] for call in calls] == ["bdh_query", "bdh_stats"]
    assert all(call["toolset"] == "bdh" for call in calls)
    assert all(call["schema"]["parameters"]["type"] == "object" for call in calls)
    assert calls[0]["schema"]["parameters"]["required"] == ["query"]


def test_stats_omits_default_vault_and_encodes_explicit_id(monkeypatch):
    endpoints = []

    def fake_request(endpoint, **kwargs):
        endpoints.append(endpoint)
        return {}

    monkeypatch.setattr(bridge, "_bdh_request", fake_request)
    bridge._tool_bdh_stats({})
    bridge._tool_bdh_stats({"vault_id": "research vault"})
    assert endpoints == ["/api/stats", "/api/stats?vault_id=research%20vault"]


# ---------------------------------------------------------------------------
# v0.5.0 — Query rewrite pipeline tests
# ---------------------------------------------------------------------------

def test_extract_context_from_conversation_history():
    history = [
        {"role": "user", "content": "Come funziona il bridge?"},
        {"role": "assistant", "content": "Il bridge usa pre_llm_call per..."},
        {"role": "user", "content": "Senti ma pensavo ad una cosa..."},
    ]
    context = bridge._extract_context(history, n=6, max_chars=200)
    assert "[user]" in context
    assert "[assistant]" in context
    assert "Come funziona il bridge?" in context
    assert "Senti ma pensavo" in context


def test_extract_context_handles_empty_history():
    assert bridge._extract_context(None) == ""
    assert bridge._extract_context([]) == ""
    assert bridge._extract_context("not a list") == ""


def test_extract_context_handles_anthropic_block_format():
    history = [
        {"role": "user", "content": [{"type": "text", "text": "Hello there"}]},
    ]
    context = bridge._extract_context(history, n=3, max_chars=200)
    assert "Hello there" in context


def test_extract_context_truncates_long_messages():
    long_msg = "A" * 500
    history = [{"role": "user", "content": long_msg}]
    context = bridge._extract_context(history, n=3, max_chars=50)
    assert len(context) < 100  # [user] prefix + 50 chars


def test_rewrite_query_returns_none_without_api_key(monkeypatch):
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "")
    monkeypatch.delenv("BDH_REWRITE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    result = bridge._rewrite_query("test message")
    assert result is None


def test_rewrite_query_resolves_key_after_module_import(monkeypatch):
    """launchd/.env loading may happen after plugin module import."""
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "")
    monkeypatch.setenv("BDH_REWRITE_API_KEY", "runtime-key")
    assert bridge._current_rewrite_api_key() == "runtime-key"


def test_rewrite_query_parses_valid_json_response(monkeypatch):
    """Simulate a valid LLM response and verify parsing."""
    fake_response_body = json.dumps({
        "choices": [{
            "message": {
                "content": '{"should_query": true, "query": "BDH bridge query rewrite pipeline", "search_query": "BDH bridge query rewrite pipeline", "sub_queries": ["LLM preprocessing", "context recovery"]}'
            }
        }]
    })

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=None):
        return FakeResp(fake_response_body)

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("senti ma pensavo ad una cosa sul bridge")
    assert result is not None
    assert result["should_query"] is True
    assert "BDH bridge" in result["query"]
    assert len(result["sub_queries"]) == 2


def test_rewrite_query_handles_should_query_false(monkeypatch):
    """When LLM says should_query=false, the result reflects that."""
    fake_response_body = json.dumps({
        "choices": [{
            "message": {
                "content": '{"should_query": false, "query": "riavvia il gateway", "sub_queries": []}'
            }
        }]
    })

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=None):
        return FakeResp(fake_response_body)

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("riavvia il gateway")
    assert result is not None
    assert result["should_query"] is False


def test_rewrite_query_strips_markdown_code_fences(monkeypatch):
    """LLM might wrap JSON in ```json ... ``` — we strip it."""
    fake_response_body = json.dumps({
        "choices": [{
            "message": {
                "content": '```json\n{"should_query": true, "query": "test", "sub_queries": []}\n```'
            }
        }]
    })

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=None):
        return FakeResp(fake_response_body)

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("test message")
    assert result is not None
    assert result["should_query"] is True


def test_rewrite_query_falls_back_on_timeout(monkeypatch):
    """On network timeout, _rewrite_query returns None (fallback to raw)."""
    from urllib.error import URLError

    def fake_urlopen(req, timeout=None):
        raise URLError("timed out")

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("test message")
    assert result is None


def test_rewrite_query_falls_back_on_invalid_json(monkeypatch):
    """When LLM returns garbage, _rewrite_query returns None."""
    fake_response_body = json.dumps({
        "choices": [{
            "message": {"content": "This is not JSON at all"}
        }]
    })

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=None):
        return FakeResp(fake_response_body)

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("test message")
    assert result is None


def test_pre_llm_with_rewrite_disabled_uses_mechanical_gate(monkeypatch):
    """When BDH_QUERY_REWRITE_ENABLED is false, behavior is v0.4.0."""
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *a, **kw: {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "ctx",
        },
    )
    result = bridge._on_pre_llm_call(
        session_id="s1",
        user_message="Come avevamo risolto quel bug del gateway?",
    )
    assert result and "context" in result


def test_pre_llm_with_rewrite_skip_when_classification_false(monkeypatch):
    """When LLM says should_query=false, skip BDH entirely."""
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda msg, ctx="": {"should_query": False, "query": "", "sub_queries": []},
    )
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("BDH should not be queried")),
    )
    result = bridge._on_pre_llm_call(
        session_id="s1",
        user_message="riavvia il gateway",
    )
    assert result is None


def test_pre_llm_with_rewrite_uses_rewritten_query(monkeypatch):
    """When LLM rewrites the query, BDH receives the search_query version."""
    captured = []

    def fake_sync(query, **kwargs):
        captured.append(query)
        return {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "ctx",
        }

    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda msg, ctx="": {"should_query": True, "query": "rewritten technical query", "search_query": "english technical query", "sub_queries": []},
    )
    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_sync)
    bridge._on_pre_llm_call(
        session_id="s1",
        user_message="senti ma pensavo ad una cosa sul bridge",
        conversation_history=[{"role": "user", "content": "precedente"}],
    )
    assert captured[0] == "rewritten technical query"


def test_pre_llm_falls_back_on_rewrite_failure(monkeypatch):
    """When rewrite LLM fails, fall back to mechanical gate + raw message."""
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(bridge, "_rewrite_query", lambda msg, ctx="": None)

    def fake_sync(query, **kwargs):
        assert query == "Come avevamo risolto quel bug del gateway?"  # raw, not rewritten
        return {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "ctx",
        }

    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_sync)
    result = bridge._on_pre_llm_call(
        session_id="s1",
        user_message="Come avevamo risolto quel bug del gateway?",
    )
    assert result and "context" in result


def test_post_api_uses_rewritten_query_as_seed(monkeypatch):
    """Write path uses the user-language query (not search_query) when available."""
    captured = []

    def fake_async(query, **kwargs):
        captured.append(query)

    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)
    state_kwargs = {"session_id": "s1"}
    bridge._remember_turn_state(state_kwargs, "senti ma pensavo ad una cosa sul bridge")
    bridge._update_turn_state(state_kwargs, "BDH bridge query rewrite pipeline", True)
    bridge._on_post_api_request(
        session_id="s1",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
    assert captured[0] == "BDH bridge query rewrite pipeline"


def test_post_api_skips_write_when_classification_false(monkeypatch):
    """When classification said should_query=false, skip the write too."""
    monkeypatch.setattr(
        bridge,
        "_bdh_query_async",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("write should be skipped")),
    )
    state_kwargs = {"session_id": "s1"}
    bridge._remember_turn_state(state_kwargs, "riavvia il gateway")
    bridge._update_turn_state(state_kwargs, "", False)
    bridge._on_post_api_request(
        session_id="s1",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )


def test_post_api_uses_its_own_interleaved_session_turn_state(monkeypatch):
    """A's post-hook must not inherit B's rewrite or false classification."""
    writes = []
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda message, context="": {
            "should_query": message == "A user message",
            "query": f"rewrite for {message}",
            "search_query": f"search for {message}",
            "sub_queries": [],
        },
    )
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "context",
        },
    )
    monkeypatch.setattr(
        bridge, "_bdh_query_async", lambda query, **kwargs: writes.append((query, kwargs))
    )

    bridge._on_pre_llm_call(session_id="A", user_message="A user message")
    bridge._on_pre_llm_call(session_id="B", user_message="B user message")
    bridge._on_post_api_request(
        session_id="A",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "assistant response A"})(),
    )

    assert len(writes) == 1
    assert writes[0][0] == "rewrite for A user message"
    assert writes[0][1]["user_prompt"] == "assistant response A"
    assert writes[0][1]["source"] == "assistant_response"
    # on_success is present — it's the session synthesis callback.
    assert callable(writes[0][1].get("on_success"))


def test_pre_hook_evicts_expired_turn_state_without_evicting_live_turn(monkeypatch):
    """A missing post-hook expires, while a live turn remains available to post."""
    writes = []
    now = [100.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    monkeypatch.setattr(bridge, "_TURN_STATE_TTL_SECONDS", 10, raising=False)
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda message, context="": {
            "should_query": True,
            "query": f"rewrite for {message}",
            "search_query": f"search for {message}",
            "sub_queries": [],
        },
    )
    monkeypatch.setattr(
        bridge,
        "_bdh_query_sync",
        lambda *args, **kwargs: {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "context",
        },
    )
    monkeypatch.setattr(bridge, "_bdh_query_async", lambda query, **kwargs: writes.append(query))

    bridge._on_pre_llm_call(session_id="expired", user_message="expired message")
    now[0] += 11
    bridge._on_pre_llm_call(session_id="live", user_message="live message")
    bridge._on_post_api_request(
        session_id="expired",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "late response"})(),
    )
    bridge._on_post_api_request(
        session_id="live",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "live response"})(),
    )

    assert writes == ["rewrite for live message"]


# ---------------------------------------------------------------------------
# v0.6.0 — Multi-query variant normalization tests
# ---------------------------------------------------------------------------

def _fake_urlopen_for_content(monkeypatch, content_obj):
    """Helper: patch urlopen so _rewrite_query sees `content_obj` as JSON."""
    fake_response_body = json.dumps({
        "choices": [{"message": {"content": json.dumps(content_obj)}}]
    })

    class FakeResp:
        def __init__(self, body):
            self._body = body.encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(
        bridge.urllib.request,
        "urlopen",
        lambda req, timeout=None: FakeResp(fake_response_body),
    )


def test_normalize_query_variants_accepts_search_queries_array(monkeypatch):
    """Provider-specific `search_queries` array is normalized to sub_queries."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "italian query",
        "search_queries": ["english query", "keywords query"],
    })
    result = bridge._rewrite_query("senti ma...")
    assert result is not None
    assert result["query"] == "italian query"
    assert result["search_query"] == "english query"
    assert result["sub_queries"] == ["keywords query"]


def test_normalize_query_variants_accepts_query_variants_alias(monkeypatch):
    """Provider-specific `query_variants` alias is also accepted."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "italian query",
        "query_variants": ["variant a", "variant b"],
    })
    result = bridge._rewrite_query("senti ma...")
    assert result is not None
    assert result["search_query"] == "variant a"
    assert result["sub_queries"] == ["variant b"]


def test_normalize_query_variants_preserves_legacy_search_query(monkeypatch):
    """Legacy `search_query` string keeps working and is preferred as first variant."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "italian query",
        "search_query": "legacy english query",
        "sub_queries": ["sub one", "sub two"],
    })
    result = bridge._rewrite_query("senti ma...")
    assert result is not None
    assert result["search_query"] == "legacy english query"
    assert result["sub_queries"] == ["sub one", "sub two"]


def test_normalize_query_variants_merges_all_sources(monkeypatch):
    """search_query + search_queries + query_variants + sub_queries all merge."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "q",
        "search_query": "first",
        "search_queries": ["second", "third"],
        "query_variants": ["fourth"],
        "sub_queries": ["fifth", "sixth"],
    })
    result = bridge._rewrite_query("msg")
    assert result["search_query"] == "first"
    assert result["sub_queries"] == ["second", "third", "fourth", "fifth", "sixth"]


def test_normalize_query_variants_filters_empty_and_non_strings(monkeypatch):
    """Empty strings, non-strings, and duplicates are filtered from variants."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "q",
        "search_queries": ["", "   ", "valid", 123, None, "valid"],
    })
    result = bridge._rewrite_query("msg")
    assert result["search_query"] == "valid"
    assert result["sub_queries"] == []


def test_normalize_query_variants_deduplicates_case_insensitively(monkeypatch):
    """Case-insensitive duplicates are dropped while preserving order."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "q",
        "search_query": "Hello World",
        "search_queries": ["hello world", "HELLO WORLD", "other"],
    })
    result = bridge._rewrite_query("msg")
    assert result["search_query"] == "Hello World"
    assert result["sub_queries"] == ["other"]


def test_normalize_query_variants_truncates_over_limit(monkeypatch):
    """Variants beyond max_variants are dropped with a debug log."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "q",
        "search_queries": [f"v{i}" for i in range(20)],
    })
    monkeypatch.setattr(bridge, "_REWRITE_MAX_VARIANTS", 5)
    result = bridge._rewrite_query("msg")
    assert result["search_query"] == "v0"
    assert result["sub_queries"] == ["v1", "v2", "v3", "v4"]
    assert all(v not in result["sub_queries"] for v in [f"v{i}" for i in range(5, 20)])


def test_normalize_query_variants_accepts_should_query_false_without_query(monkeypatch):
    """A rewrite rejection is valid even when the provider omits rewrite fields."""
    _fake_urlopen_for_content(monkeypatch, {"should_query": False})

    result = bridge._rewrite_query("non cercare nulla nel vault")

    assert result == {
        "should_query": False,
        "query": "non cercare nulla nel vault",
        "search_query": "non cercare nulla nel vault",
        "sub_queries": [],
    }


def test_normalize_query_variants_falls_back_when_query_missing(monkeypatch):
    """Malformed rewrite output (missing query) triggers full fallback."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "search_queries": ["only search"],
    })
    result = bridge._rewrite_query("msg")
    assert result is None


def test_normalize_query_variants_falls_back_when_query_empty(monkeypatch):
    """Empty query string triggers full fallback."""
    _fake_urlopen_for_content(monkeypatch, {
        "should_query": True,
        "query": "   ",
        "search_query": "english",
    })
    result = bridge._rewrite_query("msg")
    assert result is None


def test_pre_llm_uses_normalized_search_query_for_retrieval(monkeypatch):
    """Read path uses the normalized search_query, not the raw provider array."""
    captured = []

    def fake_sync(query, **kwargs):
        captured.append(query)
        return {
            "activated_notes": [{"id": "n1", "title": "Test", "score": 0.9}],
            "routing": {"hybrid_top_score": 0.8, "vector_top_score": 0.7, "bm25_matched_term_count": 3},
            "response": "ctx",
        }

    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "fake-key")
    monkeypatch.setattr(
        bridge,
        "_rewrite_query",
        lambda msg, ctx="": {
            "should_query": True,
            "query": "rewritten technical query",
            "search_query": "english technical query",
            "sub_queries": ["related topic"],
        },
    )
    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_sync)
    bridge._on_pre_llm_call(
        session_id="s1",
        user_message="senti ma pensavo ad una cosa sul bridge",
    )
    assert captured[0] == "rewritten technical query"


def test_post_api_uses_original_query_for_write_with_variants(monkeypatch):
    """Write path keeps the user-language query even when variants are normalized."""
    captured = []

    def fake_async(query, **kwargs):
        captured.append(query)

    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)
    state_kwargs = {"session_id": "s1"}
    bridge._remember_turn_state(state_kwargs, "senti ma pensavo ad una cosa sul bridge")
    bridge._update_turn_state(state_kwargs, "BDH bridge query rewrite pipeline", True)
    bridge._on_post_api_request(
        session_id="s1",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
    assert captured[0] == "BDH bridge query rewrite pipeline"


# ---------------------------------------------------------------------------
# v0.7.0: session-end synthesis
# ---------------------------------------------------------------------------

def _enable_synth(monkeypatch, min_turns=3):
    monkeypatch.setattr(bridge, "_SESSION_SYNTH_ENABLED", True)
    monkeypatch.setattr(bridge, "_SESSION_SYNTH_MIN_TURNS", min_turns)
    bridge._session_buffers.clear()
    bridge._flushed_sessions.clear()
    bridge._session_pending_writes.clear()
    bridge._session_finalize_requested.clear()


def _complete_fake_write(kwargs, *, success=True):
    """Run the production callback order for a synchronous test double."""
    if success:
        callback = kwargs.get("on_success")
        if callback:
            callback()
    complete = kwargs.get("on_complete")
    if complete:
        complete()


def test_session_buffer_accumulates_written_turns(monkeypatch):
    """Turns are buffered per session only when the write path succeeded."""
    _enable_synth(monkeypatch)
    # Simulate successful HTTP write — on_success callback must fire.
    def fake_async_ok(query, **kwargs):
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_ok)

    state_kwargs = {"session_id": "synth-sess"}
    bridge._remember_turn_state(state_kwargs, "user q1")
    bridge._on_post_api_request(
        session_id="synth-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer 1"})(),
    )
    bridge._remember_turn_state(state_kwargs, "user q2")
    bridge._on_post_api_request(
        session_id="synth-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer 2"})(),
    )

    buf = bridge._session_buffers.get("synth-sess", [])
    assert len(buf) == 2
    assert buf[0]["user"] == "user q1"
    assert buf[1]["assistant"] == "answer 2"


def test_session_finalize_flushes_synthesis(monkeypatch):
    """on_session_finalize synthesises the targeted session."""
    _enable_synth(monkeypatch, min_turns=2)
    synth_calls = []
    # Track all _bdh_query_async calls; fire on_success for per-turn writes
    # so _remember_session_turn runs and populates the buffer.
    def fake_async_with_synth(*args, **kwargs):
        synth_calls.append(kwargs)
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_with_synth)
    # Filter to the synthesis lane only; the per-turn write path also calls it.
    def synth_only():
        return [c for c in synth_calls if c.get("source") == "session_synthesis"]

    # Write two turns in session "a".
    state_a = {"session_id": "a"}
    for msg, ans in [("q1", "a1"), ("q2", "a2")]:
        bridge._remember_turn_state(state_a, msg)
        bridge._on_post_api_request(
            session_id="a", finish_reason="stop",
            assistant_message=type("Message", (), {"content": ans})(),
        )

    # Finalize session "a" -> flushes "a".
    bridge._on_session_finalize(session_id="a")

    synths = synth_only()
    assert len(synths) == 1
    kw = synths[0]
    assert kw.get("source") == "session_synthesis"
    assert "USER: q1" in kw.get("user_prompt", "")
    assert "ASSISTANT: a2" in kw.get("user_prompt", "")
    # The "a" buffer is drained after the flush.
    assert "a" not in bridge._session_buffers


def test_session_finalize_below_min_turns_skips(monkeypatch):
    """Short sessions below min turns are not synthesised on finalize."""
    _enable_synth(monkeypatch, min_turns=5)
    synth_calls = []
    monkeypatch.setattr(
        bridge, "_bdh_query_async",
        lambda *a, **kw: synth_calls.append(kw),
    )

    state_a = {"session_id": "a"}
    bridge._remember_turn_state(state_a, "q1")
    bridge._on_post_api_request(
        session_id="a", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "a1"})(),
    )

    bridge._on_session_finalize(session_id="a")
    synth_calls = [c for c in synth_calls if c.get("source") == "session_synthesis"]
    assert synth_calls == []
    # Buffer was still drained (no stale synthesis later).
    assert "a" not in bridge._session_buffers


# ---------------------------------------------------------------------------
# v0.7.1: issue #14 regression tests — lifecycle hooks, interleaving, no dup
# ---------------------------------------------------------------------------

def test_interleaved_sessions_no_premature_flush(monkeypatch):
    """Issue #14: sessions A/B interleaving must not flush a live session.

    Scenario: A turn, B pre-hook, A turn, B pre-hook.  Under the old
    process-global _last_session_key heuristic, B's pre-hook would flush A
    each time B appeared.  With lifecycle hooks, A is only flushed when
    Hermes explicitly fires on_session_finalize or on_session_reset.
    """
    _enable_synth(monkeypatch, min_turns=2)
    # Disable rewrite so the mechanical gate + raw message path is used;
    # this avoids network dependency and keeps the test deterministic.
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", False)
    synth_calls = []

    def fake_async_with_synth(*args, **kwargs):
        synth_calls.append(kwargs)
        _complete_fake_write(kwargs)
    monkeypatch.setattr(
        bridge, "_bdh_query_async", fake_async_with_synth,
    )
    monkeypatch.setattr(
        bridge, "_bdh_query_sync",
        lambda *a, **kw: {
            "activated_notes": [], "response": "",
        },
    )

    def synth_only():
        return [c for c in synth_calls if c.get("source") == "session_synthesis"]

    # Turn 1 in session A
    bridge._on_pre_llm_call(session_id="A", user_message="A first question")
    bridge._on_post_api_request(
        session_id="A", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer A1"})(),
    )

    # Turn 1 in session B (interleaving)
    bridge._on_pre_llm_call(session_id="B", user_message="B first question")
    bridge._on_post_api_request(
        session_id="B", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer B1"})(),
    )

    # Turn 2 in session A — A is still alive, must NOT be flushed
    bridge._on_pre_llm_call(session_id="A", user_message="A second question")
    bridge._on_post_api_request(
        session_id="A", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer A2"})(),
    )

    # No lifecycle events yet — zero synthesis calls
    assert synth_only() == []

    # Turn 2 in session B
    bridge._on_pre_llm_call(session_id="B", user_message="B second question")
    bridge._on_post_api_request(
        session_id="B", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer B2"})(),
    )

    # Still no lifecycle events — zero synthesis
    assert synth_only() == []

    # Now finalize A — only A should flush
    bridge._on_session_finalize(session_id="A")
    synths = synth_only()
    assert len(synths) == 1
    assert "USER: A first question" in synths[0]["user_prompt"]
    assert "ASSISTANT: answer A2" in synths[0]["user_prompt"]
    # B must still be buffered
    assert "B" in bridge._session_buffers

    # Finalize B — only B flushes
    bridge._on_session_finalize(session_id="B")
    synths = synth_only()
    assert len(synths) == 2
    assert "USER: B first question" in synths[1]["user_prompt"]
    assert "ASSISTANT: answer B2" in synths[1]["user_prompt"]
    # Both drained
    assert "A" not in bridge._session_buffers
    assert "B" not in bridge._session_buffers


def test_session_reset_flushes_old_session(monkeypatch):
    """on_session_reset flushes old_session_id, not the new one."""
    _enable_synth(monkeypatch, min_turns=2)
    synth_calls = []

    def fake_async_with_synth(*args, **kwargs):
        synth_calls.append(kwargs)
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_with_synth)

    def synth_only():
        return [c for c in synth_calls if c.get("source") == "session_synthesis"]

    state_old = {"session_id": "old"}
    bridge._remember_turn_state(state_old, "old q1")
    bridge._on_post_api_request(
        session_id="old", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "old a1"})(),
    )
    bridge._remember_turn_state(state_old, "old q2")
    bridge._on_post_api_request(
        session_id="old", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "old a2"})(),
    )

    # Simulate /new or policy expiry reset
    bridge._on_session_reset(old_session_id="old", new_session_id="new")

    synths = synth_only()
    assert len(synths) == 1
    assert "USER: old q1" in synths[0]["user_prompt"]
    assert "ASSISTANT: old a2" in synths[0]["user_prompt"]
    assert "old" not in bridge._session_buffers

    # The new session should not have been flushed
    bridge._remember_turn_state({"session_id": "new"}, "new q1")
    bridge._on_post_api_request(
        session_id="new", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "new a1"})(),
    )
    assert "new" in bridge._session_buffers
    # Only 1 synthesis so far (for "old")
    assert len(synth_only()) == 1


def test_no_duplicate_flush_finalize_then_reset(monkeypatch):
    """A session finalized and then reset must only flush once."""
    _enable_synth(monkeypatch, min_turns=2)
    synth_calls = []

    def fake_async_with_synth(*args, **kwargs):
        synth_calls.append(kwargs)
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_with_synth)

    def synth_only():
        return [c for c in synth_calls if c.get("source") == "session_synthesis"]

    state = {"session_id": "s1"}
    bridge._remember_turn_state(state, "q1")
    bridge._on_post_api_request(
        session_id="s1", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "a1"})(),
    )
    bridge._remember_turn_state(state, "q2")
    bridge._on_post_api_request(
        session_id="s1", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "a2"})(),
    )

    # Finalize first
    bridge._on_session_finalize(session_id="s1")
    assert len(synth_only()) == 1
    assert "s1" not in bridge._session_buffers

    # Reset with same old_session_id — must NOT flush again
    bridge._on_session_reset(old_session_id="s1", new_session_id="s2")
    assert len(synth_only()) == 1  # still 1, not 2


def test_session_finalize_waits_for_pending_async_write(monkeypatch):
    """Issue #17: finalization cannot outrun the last async write."""
    _enable_synth(monkeypatch, min_turns=1)
    calls = []
    pending = []

    def fake_async(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("source") == "assistant_response":
            pending.append(kwargs)

    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)
    state = {"session_id": "race-sess"}
    bridge._remember_turn_state(state, "last question")
    bridge._on_post_api_request(
        session_id="race-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "last answer"})(),
    )
    assert len(pending) == 1

    # The lifecycle event arrives before BDH confirms the write.
    bridge._on_session_finalize(session_id="race-sess")
    assert [c for c in calls if c.get("source") == "session_synthesis"] == []
    assert bridge._session_pending_writes["race-sess"] == 1

    # Success makes the turn eligible, but completion is the barrier release.
    pending[0]["on_success"]()
    assert [c for c in calls if c.get("source") == "session_synthesis"] == []
    pending[0]["on_complete"]()

    syntheses = [c for c in calls if c.get("source") == "session_synthesis"]
    assert len(syntheses) == 1
    assert "USER: last question" in syntheses[0]["user_prompt"]
    assert "ASSISTANT: last answer" in syntheses[0]["user_prompt"]
    assert "race-sess" not in bridge._session_pending_writes


def test_finalize_noop_when_session_never_wrote(monkeypatch):
    """Finalize on a session that never accumulated turns is a clean noop."""
    _enable_synth(monkeypatch, min_turns=1)
    synth_calls = []
    monkeypatch.setattr(
        bridge, "_bdh_query_async",
        lambda *a, **kw: synth_calls.append(kw),
    )
    bridge._on_session_finalize(session_id="never-existed")
    assert [c for c in synth_calls if c.get("source") == "session_synthesis"] == []


def test_register_wires_session_lifecycle_hooks():
    """register() exposes on_session_finalize and on_session_reset."""
    hooks = []

    class FakeApp:
        def register_hook(self, name, fn):
            hooks.append(name)

        def register_tool(self, **kwargs):
            pass

    bridge.register(FakeApp())
    assert "on_session_finalize" in hooks
    assert "on_session_reset" in hooks


# ---------------------------------------------------------------------------
# v0.7.1 — async-write success callback regression tests (issue #15)
# ---------------------------------------------------------------------------


def test_write_success_buffers_session_turn(monkeypatch):
    """Session buffer is populated only when the async write succeeds."""
    _enable_synth(monkeypatch)
    # Simulate a successful HTTP write: the worker gets a result dict.
    def fake_async_success(query, **kwargs):
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_success)

    state_kwargs = {"session_id": "ok-sess"}
    bridge._remember_turn_state(state_kwargs, "user question")
    bridge._on_post_api_request(
        session_id="ok-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "assistant answer"})(),
    )
    buf = bridge._session_buffers.get("ok-sess", [])
    assert len(buf) == 1
    assert buf[0]["user"] == "user question"
    assert buf[0]["assistant"] == "assistant answer"


def test_write_failure_does_not_buffer_session_turn(monkeypatch):
    """A failed async write must NOT enter the session synthesis buffer."""
    _enable_synth(monkeypatch)
    # Simulate a failed HTTP write: on_success is never called, completion is.
    def fake_async_failure(query, **kwargs):
        _complete_fake_write(kwargs, success=False)
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async_failure)

    state_kwargs = {"session_id": "fail-sess"}
    bridge._remember_turn_state(state_kwargs, "user question")
    bridge._on_post_api_request(
        session_id="fail-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "assistant answer"})(),
    )
    buf = bridge._session_buffers.get("fail-sess", [])
    assert len(buf) == 0


def test_write_recovery_after_failure(monkeypatch):
    """After a failed write, a subsequent successful write still buffers."""
    _enable_synth(monkeypatch)
    call_count = [0]

    def flaky_async(query, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: HTTP failure — on_success not called, completion is.
            _complete_fake_write(kwargs, success=False)
            return
        # Second call: HTTP success — fire both callbacks.
        _complete_fake_write(kwargs)
    monkeypatch.setattr(bridge, "_bdh_query_async", flaky_async)

    # Turn 1: fails
    state1 = {"session_id": "recover-sess"}
    bridge._remember_turn_state(state1, "failed question")
    bridge._on_post_api_request(
        session_id="recover-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "failed answer"})(),
    )
    assert len(bridge._session_buffers.get("recover-sess", [])) == 0

    # Turn 2: succeeds
    state2 = {"session_id": "recover-sess"}
    bridge._remember_turn_state(state2, "recovered question")
    bridge._on_post_api_request(
        session_id="recover-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "recovered answer"})(),
    )
    buf = bridge._session_buffers.get("recover-sess", [])
    assert len(buf) == 1
    assert buf[0]["user"] == "recovered question"


def test_on_success_callback_exception_does_not_crash(monkeypatch):
    """A buggy on_success callback is caught, not propagated."""
    _enable_synth(monkeypatch)

    def exploding_callback():
        raise RuntimeError("kaboom")

    def fake_async(query, **kwargs):
        cb = kwargs.get("on_success")
        if cb:
            cb()
    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)

    state_kwargs = {"session_id": "crash-sess"}
    bridge._remember_turn_state(state_kwargs, "q")
    # Must not raise even though the callback explodes.
    bridge._on_post_api_request(
        session_id="crash-sess", finish_reason="stop",
        assistant_message=type("Message", (), {"content": "a"})(),
    )


def test_real_async_success_fires_callback(monkeypatch):
    """Real _bdh_query_async calls on_success after a successful HTTP response."""
    _enable_synth(monkeypatch)
    callback_fired = threading.Event()

    def fake_request(endpoint, data=None, **kwargs):
        return {"response": "ok"}  # Simulate HTTP success

    monkeypatch.setattr(bridge, "_bdh_request", fake_request)

    # Directly call the real _bdh_query_async with a mock _bdh_request.
    bridge._bdh_query_async(
        "test query", on_success=lambda: callback_fired.set(),
    )
    assert callback_fired.wait(timeout=2), "on_success callback was not fired"


def test_real_async_failure_skips_callback(monkeypatch):
    """Real _bdh_query_async does NOT call on_success when HTTP fails."""
    _enable_synth(monkeypatch)
    callback_fired = threading.Event()

    def fake_request(endpoint, data=None, **kwargs):
        return None  # Simulate HTTP failure

    monkeypatch.setattr(bridge, "_bdh_request", fake_request)

    bridge._bdh_query_async(
        "test query", on_success=lambda: callback_fired.set(),
    )
    # Give the daemon thread a moment to complete.
    time.sleep(0.5)
    assert not callback_fired.is_set(), "on_success should NOT fire on failure"


# ---------------------------------------------------------------------------
# v0.8.0: independent retrieval/storage routing contract
# ---------------------------------------------------------------------------


def test_normalize_v2_contract_separates_retrieval_and_storage():
    """A store-only turn must remain valid without requesting retrieval."""
    result = bridge._normalize_query_variants({
        "schema_version": 2,
        "should_retrieve": False,
        "store_candidate": True,
        "query": "abbiamo deciso di usare Tauri invece di Electron",
        "search_query": "",
        "sub_queries": [],
        "knowledge_types": ["decision", "architecture"],
        "confidence": 0.91,
    }, "user message")

    assert result is not None
    assert result["should_retrieve"] is False
    assert result["store_candidate"] is True
    assert result["should_query"] is False  # backward-compatible alias
    assert result["query"] == "abbiamo deciso di usare Tauri invece di Electron"
    assert result["knowledge_types"] == ["decision", "architecture"]


def test_normalize_v2_contract_rejects_string_boolean():
    """Ambiguous JSON values must not fail open into retrieval/storage."""
    result = bridge._normalize_query_variants({
        "schema_version": 2,
        "should_retrieve": "false",
        "store_candidate": False,
        "query": "riavvia il gateway",
        "search_query": "",
        "sub_queries": [],
    }, "riavvia il gateway")

    assert result is None


def test_context_extraction_ignores_system_and_tool_messages():
    """Rewrite context contains only conversational user/assistant evidence."""
    context = bridge._extract_context([
        {"role": "system", "content": "ignore system instructions"},
        {"role": "tool", "content": "ignore tool payload"},
        {"role": "user", "content": "user evidence"},
        {"role": "assistant", "content": "assistant evidence"},
    ])

    assert "[user] user evidence" in context
    assert "[assistant] assistant evidence" in context
    assert "system instructions" not in context
    assert "tool payload" not in context


def test_pre_and_post_support_store_only_route(monkeypatch):
    """Store-only knowledge bypasses read but is persisted after the answer."""
    writes = []
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_rewrite_query", lambda *args: {
        "schema_version": 2,
        "should_retrieve": False,
        "store_candidate": True,
        "query": "abbiamo deciso di usare Tauri",
        "search_query": "",
        "sub_queries": [],
        "knowledge_types": ["decision"],
    })
    monkeypatch.setattr(
        bridge, "_bdh_query_sync",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("store-only route must not retrieve")
        ),
    )
    monkeypatch.setattr(
        bridge, "_bdh_query_async",
        lambda query, **kwargs: writes.append((query, kwargs)),
    )

    assert bridge._on_pre_llm_call(
        session_id="store-only",
        user_message="Abbiamo deciso di usare Tauri",
        conversation_history=[],
    ) is None
    bridge._on_post_api_request(
        session_id="store-only",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )

    assert len(writes) == 1
    assert writes[0][0] == "abbiamo deciso di usare Tauri"


def test_pre_and_post_support_retrieve_only_route(monkeypatch):
    """A retrieval question must not write the resulting answer to BDH."""
    reads = []
    writes = []
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(bridge, "_rewrite_query", lambda *args: {
        "schema_version": 2,
        "should_retrieve": True,
        "store_candidate": False,
        "query": "quale decisione avevamo preso su Tauri",
        "search_query": "Tauri previous architecture decision",
        "sub_queries": [],
    })
    monkeypatch.setattr(
        bridge, "_bdh_query_sync",
        lambda query, **kwargs: (
            reads.append((query, kwargs)) or {
                "routing": {
                    "hybrid_top_score": 0.8,
                    "vector_top_score": 0.8,
                    "bm25_matched_term_count": 2,
                },
                "activated_notes": [{"id": "n1", "title": "Tauri", "score": 0.9}],
                "response": "context",
            }
        ),
    )
    monkeypatch.setattr(
        bridge, "_bdh_query_async",
        lambda query, **kwargs: writes.append((query, kwargs)),
    )

    result = bridge._on_pre_llm_call(
        session_id="retrieve-only",
        user_message="Quale decisione avevamo preso su Tauri?",
        conversation_history=[],
    )
    bridge._on_post_api_request(
        session_id="retrieve-only",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )

    assert result and "context" in result
    assert reads[0][0] == "quale decisione avevamo preso su Tauri"
    assert writes == []


def test_length_finished_response_is_not_stored(monkeypatch):
    """A truncated model response must not enter durable BDH learning."""
    writes = []
    monkeypatch.setattr(
        bridge, "_bdh_query_async",
        lambda query, **kwargs: writes.append((query, kwargs)),
    )
    state_kwargs = {"session_id": "truncated"}
    bridge._remember_turn_state(state_kwargs, "durable question")
    bridge._on_post_api_request(
        session_id="truncated",
        finish_reason="length",
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )

    assert writes == []


def test_rewrite_query_parses_v2_response_from_provider(monkeypatch):
    """The provider response is normalized to the independent v2 contract."""
    _fake_urlopen_for_content(monkeypatch, {
        "schema_version": 2,
        "should_retrieve": True,
        "store_candidate": False,
        "query": "quale decisione avevamo preso su Tauri",
        "search_query": "Tauri architecture decision",
        "sub_queries": ["Tauri versus Electron"],
        "knowledge_types": ["decision", "architecture"],
        "confidence": 0.88,
    })

    result = bridge._rewrite_query("che scelta avevamo fatto per il companion?")

    assert result is not None
    assert result["schema_version"] == 2
    assert result["should_retrieve"] is True
    assert result["store_candidate"] is False
    assert result["should_query"] is True
    assert result["search_query"] == "Tauri architecture decision"
    assert result["knowledge_types"] == ["decision", "architecture"]


def test_v2_retrieval_variants_are_capped_at_three():
    """A v2 provider cannot turn one user turn into an unbounded fan-out."""
    result = bridge._normalize_query_variants({
        "schema_version": 2,
        "should_retrieve": True,
        "store_candidate": False,
        "query": "main query",
        "search_queries": ["v1", "v2", "v3", "v4"],
        "sub_queries": ["v5"],
    }, "user message")

    assert result is not None
    assert result["search_query"] == "v1"
    assert result["sub_queries"] == ["v2", "v3"]


def test_rewrite_request_disables_qwen_thinking_for_structured_output(monkeypatch):
    """oMLX/Qwen must receive explicit thinking-off template kwargs."""
    captured = {}
    body = json.dumps({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "schema_version": 2,
                    "should_retrieve": True,
                    "store_candidate": False,
                    "query": "test query",
                    "search_query": "test query",
                    "sub_queries": [],
                })
            }
        }]
    })

    class FakeResp:
        def read(self):
            return body.encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode()))
        return FakeResp()

    monkeypatch.setattr(bridge, "_REWRITE_API_KEY", "local")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    result = bridge._rewrite_query("test query")

    assert result is not None
    assert captured["chat_template_kwargs"] == {
        "enable_thinking": False,
        "thinking": False,
    }

