"""The room watcher must not have a permanent blind spot for already-idle rooms.

Regression contract for the failure observed live on 2026-09-16:

    Two rooms were authorised and held 122 and 2 events, yet the daemon never
    produced a single synthesis for either. The watcher had started AFTER they
    went quiet, so it never observed their `live -> idle` transition, and the
    transition is the only trigger. Waiting does not help: the state a room has
    to leave is the state it is already in.

The fix has two halves, and both are needed:
  1. a CONTENT gate (the ledger) so re-synthesis is decided by "has this
     transcript been submitted?", not by an observed transition;
  2. a bounded backlog pass at startup, so the blind spot is enumerated
     instead of being silent.

Half 1 alone would turn every restart into a re-submission storm; half 2 alone
would re-submit on every restart. These tests pin the pair.
"""
import json
import sqlite3

from room_synthesis_watcher import RoomSynthesisWatcher
from synthesis_ledger import SynthesisLedger


def _room_db(path, room_id="r1", members=None, events=None, updated_at=1000.0):
    db = sqlite3.connect(path)
    db.executescript("""
        drop table if exists hosted_rooms;
        drop table if exists hosted_room_events;
        create table hosted_rooms (room_id text primary key, name text,
            members_json text, updated_at real, disbanded_at real);
        create table hosted_room_events (room_id text, seq integer, kind text,
            actor_json text, payload_json text, created_at real);
    """)
    db.execute(
        "insert into hosted_rooms values (?,?,?,?,null)",
        (room_id, "Room", json.dumps(members or []), updated_at),
    )
    for seq, (kind, actor, payload, created) in enumerate(events or [], start=1):
        db.execute(
            "insert into hosted_room_events values (?,?,?,?,?,?)",
            (room_id, seq, kind, json.dumps(actor), json.dumps(payload), created),
        )
    db.commit()
    db.close()


def _quiet_room_events():
    """Three user turns, all long past the idle threshold."""
    return [
        ("message.user", {"id": "u"}, {"text": "how should the gate behave?"}, 1000.0),
        ("message.member", {"profile": "client-a"}, {"text": "by actor."}, 1001.0),
        ("message.user", {"id": "u"}, {"text": "and a mixed room?"}, 1002.0),
        ("message.member", {"profile": "client-a"}, {"text": "not a client scope."}, 1003.0),
        ("message.user", {"id": "u"}, {"text": "recovery?"}, 1004.0),
        ("message.member", {"profile": "client-a"}, {"text": "bounded."}, 1005.0),
    ]


def _watcher(tmp_path, monkeypatch, *, ledger=None, events=None, members=None):
    db_path = tmp_path / "state.db"
    _room_db(
        db_path,
        members=members if members is not None else [{"profile": "client-a"}],
        events=events if events is not None else _quiet_room_events(),
    )
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "version": 1,
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    return RoomSynthesisWatcher(
        db_path=db_path,
        state_path=tmp_path / "idle.json",
        registry_path=tmp_path / "absent-registry.json",
        dry_run=True,
        ledger=ledger if ledger is not None else SynthesisLedger(tmp_path / "ledger.json"),
    )


def test_already_idle_room_is_invisible_to_the_transition_trigger(tmp_path, monkeypatch, capsys):
    """The bug, pinned: a quiescent room never emits through the idle scan alone.

    This is not a defect in the idle watcher — it is the definition of a
    transition. It documents WHY the backlog pass has to exist.
    """
    w = _watcher(tmp_path, monkeypatch)

    # A single scan on an already-old room emits nothing, by design.
    assert w.scan_once(now=99999.0) == 0
    assert "would POST" not in capsys.readouterr().out


def test_backlog_recovers_an_authorised_already_idle_room(tmp_path, monkeypatch, capsys):
    """The recovery pass must submit a room the transition trigger cannot reach."""
    w = _watcher(tmp_path, monkeypatch)

    submitted = w.recover_backlog(now=99999.0)

    out = capsys.readouterr().out
    assert submitted == 1
    assert "would POST" in out
    assert "vault='vault-a'" in out
    assert "seq=6" in out, "the source cursor must be sent so handled messages are traceable"


def test_backlog_is_idempotent_across_restarts(tmp_path, monkeypatch, capsys):
    """Second start must not re-submit: that is the whole reason for the ledger."""
    ledger_path = tmp_path / "ledger.json"
    w = _watcher(tmp_path, monkeypatch, ledger=SynthesisLedger(ledger_path))
    assert w.recover_backlog(now=99999.0) == 1
    capsys.readouterr()

    # A brand-new process, same ledger, same unchanged room.
    restarted = _watcher(tmp_path, monkeypatch, ledger=SynthesisLedger(ledger_path))
    assert restarted.recover_backlog(now=99999.0) == 0
    assert "would POST" not in capsys.readouterr().out


def test_new_content_reopens_an_already_synthesized_room(tmp_path, monkeypatch, capsys):
    """The gate is content, not history: a new turn must be eligible again."""
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    w = _watcher(tmp_path, monkeypatch, ledger=ledger)
    assert w.recover_backlog(now=99999.0) == 1
    capsys.readouterr()

    # A further exchange arrives and then goes quiet.
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute(
        "insert into hosted_room_events values (?,?,?,?,?,?)",
        ("r1", 7, "message.user", json.dumps({"id": "u"}),
         json.dumps({"text": "does the ledger reopen it?"}), 20000.0),
    )
    con.execute(
        "insert into hosted_room_events values (?,?,?,?,?,?)",
        ("r1", 8, "message.member", json.dumps({"profile": "client-a"}),
         json.dumps({"text": "yes, new digest."}), 20001.0),
    )
    con.commit()
    con.close()

    assert w.recover_backlog(now=99999.0) == 1
    assert "would POST" in capsys.readouterr().out


def test_unauthorised_room_is_never_recovered(tmp_path, monkeypatch, capsys):
    """Recovery must not become a way around the actor gate."""
    w = _watcher(
        tmp_path, monkeypatch,
        members=[{"profile": "default"}, {"profile": "client-a"}],
    )
    assert w.recover_backlog(now=99999.0) == 0
    assert "would POST" not in capsys.readouterr().out


def test_backlog_skips_a_room_that_is_still_active(tmp_path, monkeypatch, capsys):
    """A hot room belongs to the transition trigger, not to recovery."""
    events = [
        ("message.user", {"id": "u"}, {"text": "a"}, 99990.0),
        ("message.member", {"profile": "client-a"}, {"text": "b"}, 99991.0),
        ("message.user", {"id": "u"}, {"text": "c"}, 99992.0),
        ("message.member", {"profile": "client-a"}, {"text": "d"}, 99993.0),
        ("message.user", {"id": "u"}, {"text": "e"}, 99994.0),
    ]
    w = _watcher(tmp_path, monkeypatch, events=events)
    assert w.recover_backlog(now=99999.0) == 0
    assert "would POST" not in capsys.readouterr().out


def test_backlog_limit_bounds_a_large_backlog(tmp_path, monkeypatch, capsys):
    """Recovery must be bounded: the local model costs minutes per synthesis."""
    db_path = tmp_path / "state.db"
    _room_db(db_path, room_id="r1", members=[{"profile": "client-a"}],
             events=_quiet_room_events())
    con = sqlite3.connect(db_path)
    for extra in range(2, 6):  # r2..r5, all quiet and authorised
        con.execute("insert into hosted_rooms values (?,?,?,?,null)",
                    (f"r{extra}", "Room", json.dumps([{"profile": "client-a"}]), 1000.0))
        for seq, (kind, actor, payload, created) in enumerate(_quiet_room_events(), start=1):
            con.execute("insert into hosted_room_events values (?,?,?,?,?,?)",
                        (f"r{extra}", seq, kind, json.dumps(actor),
                         json.dumps(payload), created))
    con.commit()
    con.close()

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "version": 1,
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    w = RoomSynthesisWatcher(
        db_path=db_path,
        state_path=tmp_path / "idle.json",
        registry_path=tmp_path / "absent-registry.json",
        dry_run=True,
        ledger=SynthesisLedger(tmp_path / "ledger.json"),
    )

    assert w.recover_backlog(limit=2, now=99999.0) == 2


def test_backlog_limit_zero_disables_recovery(tmp_path, monkeypatch, capsys):
    w = _watcher(tmp_path, monkeypatch)
    w.backlog_limit = 0
    assert w.recover_backlog(limit=0, now=99999.0) == 0
    assert "would POST" not in capsys.readouterr().out


def test_digest_gate_also_holds_on_the_idle_path(tmp_path, monkeypatch, capsys):
    """The idle trigger must not re-submit unchanged content either.

    A room that cycles live -> idle without new messages would otherwise
    re-synthesize the same transcript on every cycle.
    """
    ledger = SynthesisLedger(tmp_path / "ledger.json")
    w = _watcher(tmp_path, monkeypatch, ledger=ledger)

    w._on_idle("r1")  # first submission goes through and is recorded
    assert "would POST" in capsys.readouterr().out

    w._on_idle("r1")  # same transcript, no new events
    assert "would POST" not in capsys.readouterr().out
