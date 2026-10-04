"""Bounded per-turn BDH writes (issue #49, part B of #29).

Every per-turn write takes one process-wide slot. Each exit path must give the
slot back and settle the lifecycle barrier exactly once: a leaked slot silently
stops every later per-turn write for the life of the interpreter, and a missed
or doubled ``on_complete`` corrupts the session's pending-write count.
"""
import importlib.util
import threading
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bdh_bridge_per_turn_bound", Path(__file__).with_name("__init__.py")
)
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(bridge, "_current_nous_rewrite_credentials", lambda: ("", ""))
    monkeypatch.setattr(bridge, "_QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(bridge, "_bdh_per_turn_slots", threading.BoundedSemaphore(1))


def _slot_free():
    if bridge._bdh_per_turn_slots.acquire(blocking=False):
        bridge._bdh_per_turn_slots.release()
        return True
    return False


class _UnstartableThread(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


def _fail_setup(monkeypatch, sent):
    def bad_vault(_vault_id):
        raise ValueError("vault resolution failed")

    monkeypatch.setattr(bridge, "_resolve_vault_id", bad_vault)
    monkeypatch.setattr(bridge, "_bdh_request", lambda *a, **k: sent.append(a))


def test_worker_setup_failure_releases_slot_and_completes_once(monkeypatch):
    sent, completions = [], []
    _fail_setup(monkeypatch, sent)

    worker = bridge._bdh_query_async(
        "turn", source="assistant_response", on_complete=lambda: completions.append(1)
    )
    worker.join(2)

    assert completions == [1]
    assert sent == []
    assert _slot_free()


def test_thread_start_failure_releases_slot_and_leaves_completion_to_caller(monkeypatch):
    completions = []
    monkeypatch.setattr(bridge.threading, "Thread", _UnstartableThread)

    with pytest.raises(RuntimeError):
        bridge._bdh_query_async(
            "turn", source="assistant_response", on_complete=lambda: completions.append(1)
        )

    assert completions == []
    assert _slot_free()


def test_hook_settles_pending_barrier_once_when_worker_cannot_start(monkeypatch):
    monkeypatch.setattr(bridge, "_SESSION_SYNTH_ENABLED", True)
    monkeypatch.setattr(bridge, "_SESSION_SYNTH_MIN_TURNS", 3)
    bridge._session_buffers.clear()
    bridge._session_finalize_requested.clear()
    bridge._session_idle_requested.clear()
    bridge._session_pending_writes.clear()
    # Another write for the same session is still in flight: a doubled release
    # would drop it from the barrier and let finalization run too early.
    bridge._session_pending_writes["start-fail"] = 1
    bridge._remember_turn_state(
        {"session_id": "start-fail"},
        "Store this durable decision even when no worker can start.",
    )
    monkeypatch.setattr(bridge.threading, "Thread", _UnstartableThread)

    bridge._on_post_api_request(
        session_id="start-fail",
        finish_reason="stop",
        assistant_message=type("Message", (), {"content": "answer"})(),
    )

    assert bridge._session_pending_writes == {"start-fail": 1}
    assert bridge._session_buffers.get("start-fail", []) == []
    assert _slot_free()


def test_repeated_failures_never_exhaust_the_slots(monkeypatch):
    monkeypatch.setattr(bridge, "_bdh_per_turn_slots", threading.BoundedSemaphore(2))
    sent = []
    _fail_setup(monkeypatch, sent)
    for _ in range(5):
        bridge._bdh_query_async("turn", source="assistant_response").join(2)

    real_thread = threading.Thread
    monkeypatch.setattr(bridge.threading, "Thread", _UnstartableThread)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            bridge._bdh_query_async("turn", source="assistant_response")
    monkeypatch.setattr(bridge.threading, "Thread", real_thread)
    monkeypatch.setattr(bridge, "_resolve_vault_id", lambda _vault_id: None)

    landed = threading.Event()
    monkeypatch.setattr(bridge, "_bdh_request", lambda *a, **k: {"activated_notes": []})
    bridge._bdh_query_async("turn", source="assistant_response", on_success=landed.set)
    assert landed.wait(2)


def test_saturation_drop_is_counted_and_settles_the_barrier(monkeypatch):
    sent, completions = [], []
    monkeypatch.setattr(bridge, "_bdh_request", lambda *a, **k: sent.append(a))
    before = bridge._bdh_per_turn_dropped
    bridge._bdh_per_turn_slots.acquire()
    try:
        for _ in range(2):
            assert bridge._bdh_query_async(
                "turn", source="assistant_response",
                on_complete=lambda: completions.append(1),
            ) is None
    finally:
        bridge._bdh_per_turn_slots.release()

    assert completions == [1, 1]
    assert sent == []
    assert bridge._bdh_per_turn_dropped == before + 2
