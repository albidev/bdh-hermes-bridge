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
