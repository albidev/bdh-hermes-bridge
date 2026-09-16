"""The per-turn caps have ONE owner, and both synthesis paths must agree.

Two paths buffer a session turn:

``__init__.py::_remember_session_turn``   in-process, receives Hermes's final text
``session_synthesis_watcher.rebuild_turns`` standalone, reconstructs from state.db

Both truncated to a literal 1500 chars per field, hardcoded separately. A measured
agentic answer was 9858 chars, so the buffer kept the preamble and lost 85% of the
findings — on every turn answered at length, through every platform (Telegram,
Discord, TUI, cron, subagent).

Fixing one path only would have moved the loss, not removed it, and the two limits
could drift apart again: the same session would produce a different transcript
depending on which process happened to pick it up. So the caps live in the bridge
and the standalone reads them through it, exactly like the minimum-turn floor.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest

import session_synthesis_watcher as watcher_mod


def _bridge():
    return importlib.import_module("__init__")


def test_both_paths_read_the_same_caps():
    """The standalone watcher must not carry its own limit."""
    bridge = _bridge()
    assert watcher_mod._caps() == (
        bridge._SESSION_TURN_USER_MAX_CHARS,
        bridge._SESSION_TURN_ASSISTANT_MAX_CHARS,
    )


def test_caps_exceed_the_observed_answer_length():
    """A 9858-char answer is real input, not an outlier to truncate away.

    The old 1500 kept ~15% of that reply. The cap has to hold a substantive
    answer whole, or the vault learns the preamble of every finding.
    """
    bridge = _bridge()
    assert bridge._SESSION_TURN_ASSISTANT_MAX_CHARS >= 10000, (
        "the per-turn assistant cap must hold the measured 9858-char answer"
    )


def test_in_process_buffer_keeps_a_long_answer(monkeypatch):
    """The in-process path must not truncate a long answer."""
    bridge = _bridge()
    answer = "SUBSTANCE " * 1000  # 10000 chars
    with bridge._bdh_state_lock:
        bridge._session_buffers.clear()
        bridge._flushed_sessions.clear()
    monkeypatch.setattr(bridge, "_SESSION_SYNTH_ENABLED", True)

    bridge._remember_session_turn("cap-sess", "question", answer, "vault-a")

    stored = bridge._session_buffers["cap-sess"][0]["assistant"]
    assert len(stored) == len(answer), (
        f"the buffer lost {len(answer) - len(stored)} chars of a {len(answer)}-char answer"
    )


def test_persisted_buffer_uses_the_same_cap(monkeypatch, tmp_path):
    """The persisted copy is what a restart recovers from.

    A narrower limit there would silently downgrade the transcript after a
    restart, so it must match the in-memory buffer exactly.
    """
    bridge = _bridge()
    answer = "SUBSTANCE " * 1000

    captured = {}

    class Store:
        @staticmethod
        def append(session_id, entry):
            captured.update(entry)

    monkeypatch.setattr(bridge, "_SESSION_SYNTH_ENABLED", True)
    monkeypatch.setattr(bridge, "_session_buffer_store", Store())
    with bridge._bdh_state_lock:
        bridge._session_buffers.clear()
        bridge._flushed_sessions.clear()

    bridge._remember_session_turn("persist-sess", "q", answer, "vault-a")

    assert len(captured.get("assistant", "")) == len(answer)
    assert captured["assistant"] == bridge._session_buffers["persist-sess"][0]["assistant"]


def test_standalone_transcript_keeps_a_long_answer(tmp_path, monkeypatch):
    """The standalone reconstruction must keep it too."""
    answer = "SUBSTANCE " * 1000
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    db.execute("insert into sessions values ('s1','tui','default',1000.0,null)")
    db.executemany(
        "insert into messages(session_id,role,content,finish_reason,active) values (?,?,?,?,1)",
        [
            ("s1", "user", "@client-a q", None),
            ("s1", "assistant", "", "tool_calls"),
            ("s1", "assistant", answer, "stop"),
        ],
    )
    db.commit()
    db.close()

    policy = tmp_path / "p.json"
    policy.write_text(
        '{"version":1,"mention_prefixes":{"client-a":"vault-a"},"profile_vaults":{},"allow_room_registry":false}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy))
    watcher = watcher_mod.TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "i.json")
    watcher.extra_db_paths = []

    assistant = watcher.rebuild_turns("s1")[0]["assistant"]
    # `_text()` strips, so compare on the substance rather than the exact bytes:
    # the point is that nothing was truncated.
    assert assistant.count("SUBSTANCE") == answer.count("SUBSTANCE"), (
        "the standalone path must keep the whole answer"
    )
    assert len(assistant) >= len(answer) - 1


def test_caps_survive_an_unimportable_bridge(monkeypatch):
    """A loader without the package must fall back to the env, not to 1500."""
    monkeypatch.setenv("BDH_SESSION_TURN_ASSISTANT_MAX_CHARS", "9000")
    real_import = importlib.import_module

    def explode(name, *args, **kwargs):
        if name.endswith("__init__"):
            raise ImportError("simulated: bridge not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", explode)
    _, assistant_cap = watcher_mod._caps()
    assert assistant_cap == 9000


def test_garbage_cap_falls_back_above_the_old_limit(monkeypatch):
    """Malformed config must not silently reinstate the lossy 1500."""
    monkeypatch.setenv("BDH_SESSION_TURN_ASSISTANT_MAX_CHARS", "not-a-number")
    real_import = importlib.import_module

    def explode(name, *args, **kwargs):
        if name.endswith("__init__"):
            raise ImportError("simulated: bridge not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", explode)
    _, assistant_cap = watcher_mod._caps()
    assert assistant_cap >= 10000
