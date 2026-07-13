"""Tests for the BDH bridge read/write turn contract."""

import importlib.util
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
