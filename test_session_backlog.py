"""The session watcher must reach sessions the transition trigger cannot.

The blind spot, and why it is structural
---------------------------------------
`SessionIdleWatcher` emits ONLY on a live -> idle crossing. So a session is lost
when the crossing happens without anyone acting on it:

  A. it went idle while ineligible (too few turns, or no authorised vault), so the
     crossing was consumed and it stays `idle` forever;
  B. it went idle while this process was not running, so it has no recorded state
     at all.

Neither is recoverable by waiting: neither will produce another crossing. The
content is dropped in silence — no audit row, indistinguishable from "nothing to
learn". Measured on the live instance: 3 sessions in this state, one of them 12
turns of client work, against 11 sessions correctly refused by the actor gate.

The tests below pin: the backlog finds exactly the eligible ones, is idempotent
across restarts via the ledger, is bounded, and never bypasses either gate.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from session_synthesis_watcher import TranscriptIdleWatcher
from synthesis_ledger import SynthesisLedger


def _db(path, sessions):
    """sessions: list of (session_id, profile_name, [rows], last_activity)."""
    db = sqlite3.connect(path)
    db.executescript("""
        drop table if exists sessions;
        drop table if exists messages;
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    for sid, profile, rows, last in sessions:
        db.execute(
            "insert into sessions values (?, 'tui', ?, ?, null)", (sid, profile, last)
        )
        for role, content, finish in rows:
            db.execute(
                "insert into messages(session_id,role,content,finish_reason,active) "
                "values (?,?,?,?,1)",
                (sid, role, content, finish),
            )
    db.commit()
    db.close()


def _turn(user, answer):
    return [("user", user, None), ("assistant", "", "tool_calls"), ("assistant", answer, "stop")]


OLD = 1000.0  # far past any threshold


def _watcher(tmp_path, monkeypatch, sessions, ledger=None, limit=3):
    db_path = tmp_path / "state.db"
    _db(db_path, sessions)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy))
    monkeypatch.setenv("BDH_SESSION_SYNTH_MIN_TURNS", "1")
    w = TranscriptIdleWatcher(
        db_path=db_path,
        state_path=tmp_path / "idle.json",
        ledger=ledger if ledger is not None else SynthesisLedger(tmp_path / "ledger.json"),
        backlog_limit=limit,
    )
    w.extra_db_paths = []
    return w


def test_backlog_finds_an_authorised_idle_session(tmp_path, monkeypatch):
    """Case A: idle, eligible, never emitted by the live path."""
    w = _watcher(tmp_path, monkeypatch, [
        ("lost", "default", _turn("@client-a question", "ANSWER"), OLD),
    ])
    assert w.backlog(now=OLD + 10000) == ["lost"]


def test_backlog_skips_an_unauthorised_session(tmp_path, monkeypatch):
    """The actor gate still decides: no vault means no recovery either."""
    w = _watcher(tmp_path, monkeypatch, [
        ("hermes-work", "default", _turn("fix the parser", "ANSWER"), OLD),
    ])
    assert w.backlog(now=OLD + 10000) == []


def test_backlog_skips_a_session_below_the_floor(tmp_path, monkeypatch):
    """A session with no assistant answer has nothing to contribute."""
    w = _watcher(tmp_path, monkeypatch, [
        ("empty", "default", [("user", "@client-a q", None), ("assistant", "", "tool_calls")], OLD),
    ])
    assert w.backlog(now=OLD + 10000) == []


def test_backlog_skips_a_session_that_is_still_active(tmp_path, monkeypatch):
    """A hot session belongs to the transition trigger, not to recovery."""
    now = 1_000_000.0
    w = _watcher(tmp_path, monkeypatch, [
        ("active", "default", _turn("@client-a q", "ANSWER"), now - 10),
    ])
    assert w.backlog(now=now) == []


def test_backlog_is_idempotent_across_restarts(tmp_path, monkeypatch):
    """The ledger makes recovery once-only, so a restart is not a re-send."""
    ledger_path = tmp_path / "ledger.json"
    sessions = [("lost", "default", _turn("@client-a q", "ANSWER"), OLD)]

    first = _watcher(tmp_path, monkeypatch, sessions, ledger=SynthesisLedger(ledger_path))
    digest = first._digest_for(first.rebuild_turns("lost"))
    assert digest, "the digest must be derivable for an eligible session"
    first.ledger.record("lost", sha=digest)

    restarted = _watcher(tmp_path, monkeypatch, sessions, ledger=SynthesisLedger(ledger_path))
    assert restarted.backlog(now=OLD + 10000) == [], (
        "a session already on the ledger must not be recovered again"
    )


def test_backlog_limit_is_bounded_and_zero_means_zero(tmp_path, monkeypatch):
    sessions = [
        (f"s{i}", "default", _turn(f"@client-a q{i}", "ANSWER"), OLD + i)
        for i in range(5)
    ]
    w = _watcher(tmp_path, monkeypatch, sessions)
    assert len(w.backlog(limit=2, now=OLD + 10000)) == 2
    assert w.backlog(limit=0, now=OLD + 10000) == []
    assert len(w.backlog(limit=None, now=OLD + 10000)) == 5


def test_backlog_returns_newest_first(tmp_path, monkeypatch):
    """Newest-first so a bounded pass keeps the useful end of a long backlog."""
    sessions = [
        ("older", "default", _turn("@client-a q", "ANSWER"), OLD),
        ("newer", "default", _turn("@client-a q", "ANSWER"), OLD + 500),
    ]
    w = _watcher(tmp_path, monkeypatch, sessions)
    assert w.backlog(now=OLD + 10000) == ["newer", "older"]


def test_recover_backlog_goes_through_the_same_gates(tmp_path, monkeypatch):
    """Recovery must not be a way around the actor gate."""
    w = _watcher(tmp_path, monkeypatch, [
        ("hermes-work", "default", _turn("internal work", "ANSWER"), OLD),
        ("client-work", "default", _turn("@client-a q", "ANSWER"), OLD + 1),
    ])
    flushed = []

    class Stub:
        _bdh_state_lock = __import__("threading").RLock()
        _session_buffers = {}

        @staticmethod
        def _build_session_transcript(buf):
            return ("\n".join(t["user"] for t in buf), 1, 0)

        @staticmethod
        def _flush_session_synthesis(session_id, final=True, wait=False, on_success=None):
            flushed.append(session_id)
            if on_success is not None:
                # The real flush digests what _build_session_transcript returns and
                # passes THAT to on_success. The stub must mirror it, or the ledger
                # would hold a digest the gate can never match.
                import hashlib
                text, _, _ = Stub._build_session_transcript(
                    w.bridge._session_buffers.get(session_id, [])
                )
                on_success(hashlib.sha256(text.encode("utf-8")).hexdigest())

    w.bridge = Stub()

    acted = w.recover_backlog(limit=5, now=OLD + 10000)

    assert acted == 1
    assert flushed == ["client-work"], "only the authorised session may be recovered"


def test_recovery_records_the_digest_on_success(tmp_path, monkeypatch):
    """The ledger is written from the success callback, not before dispatch."""
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    w = _watcher(tmp_path, monkeypatch, [
        ("client-work", "default", _turn("@client-a q", "ANSWER"), OLD),
    ], ledger=ledger)

    class Stub:
        _bdh_state_lock = __import__("threading").RLock()
        _session_buffers = {}

        @staticmethod
        def _build_session_transcript(buf):
            return ("TRANSCRIPT BODY", 1, 0)

        @staticmethod
        def _flush_session_synthesis(session_id, final=True, wait=False, on_success=None):
            if on_success is not None:
                # Mirror the real flush: digest the builder's output.
                import hashlib
                on_success(hashlib.sha256("TRANSCRIPT BODY".encode("utf-8")).hexdigest())

    w.bridge = Stub()
    w._on_idle("client-work")

    import hashlib as _h
    assert ledger.sha_for("client-work") == _h.sha256("TRANSCRIPT BODY".encode("utf-8")).hexdigest()
    # and the second pass is now a no-op
    assert w.backlog(now=OLD + 10000) == []


def test_a_failed_post_does_not_record_the_digest(tmp_path, monkeypatch):
    """A digest written on failure would drop that transcript permanently."""
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    w = _watcher(tmp_path, monkeypatch, [
        ("client-work", "default", _turn("@client-a q", "ANSWER"), OLD),
    ], ledger=ledger)

    class Stub:
        _bdh_state_lock = __import__("threading").RLock()
        _session_buffers = {}

        @staticmethod
        def _build_session_transcript(buf):
            return ("TRANSCRIPT BODY", 1, 0)

        @staticmethod
        def _flush_session_synthesis(session_id, final=True, wait=False, on_success=None):
            pass  # the POST failed, so on_success is never called

    w.bridge = Stub()
    w._on_idle("client-work")

    assert ledger.sha_for("client-work") is None
    assert w.backlog(now=OLD + 10000) == ["client-work"], (
        "a failed submission must stay eligible for the next pass"
    )
