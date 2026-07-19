"""Tests for the BDH bridge read/write turn contract."""

import importlib.util
import json
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
    bridge._last_user_message = "Review the conversation above and update the skill library."
    bridge._on_post_api_request(
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
    bridge._last_user_message = "Review the latest project architecture and explain the changes."
    bridge._on_post_api_request(
        platform="cron",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
    assert writes == []


def test_cron_bdh_opt_in_allows_read_and_write(monkeypatch):
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

    monkeypatch.setattr(bridge, "_bdh_query_sync", fake_query)
    result = bridge._on_pre_llm_call(
        session_id="session-1",
        user_message="Come avevamo risolto quel bug del gateway?",
    )

    assert calls[0][1] == {
        "source": "automatic_retrieval",
        "timeout": 2,
        "learn": False,
        "retries": 1,
    }
    assert result and "context" in result
    assert "[BDH CONTEXT — optional]" in result["context"]
    assert "Gateway recovery" in result["context"]
    assert "Use this as supporting context." in result["context"]


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
    result = bridge._rewrite_query("test message")
    assert result is None


def test_rewrite_query_parses_valid_json_response(monkeypatch):
    """Simulate a valid LLM response and verify parsing."""
    fake_response_body = json.dumps({
        "choices": [{
            "message": {
                "content": '{"should_query": true, "query": "BDH bridge query rewrite pipeline", "sub_queries": ["LLM preprocessing", "context recovery"]}'
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
                "content": '{"should_query": false, "query": "", "sub_queries": []}'
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
    """When LLM rewrites the query, BDH receives the rewritten version."""
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
        lambda msg, ctx="": {"should_query": True, "query": "rewritten technical query", "sub_queries": []},
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
    """Write path uses the rewritten query when available."""
    captured = []

    def fake_async(query, **kwargs):
        captured.append(query)

    monkeypatch.setattr(bridge, "_bdh_query_async", fake_async)
    bridge._last_user_message = "senti ma pensavo ad una cosa sul bridge"
    bridge._last_rewritten_query = "BDH bridge query rewrite pipeline"
    bridge._last_should_query = True
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
    bridge._last_user_message = "riavvia il gateway"
    bridge._last_rewritten_query = ""
    bridge._last_should_query = False  # classification said no
    bridge._on_post_api_request(
        session_id="s1",
        finish_reason="stop",
        assistant_content_chars=300,
        assistant_message=type("Message", (), {"content": "x" * 300})(),
    )
