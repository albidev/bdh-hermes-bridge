"""Invariants for the room-synthesis watcher's actor gate and flush boundary.

Room synthesis writes to a vault, so the same two rules that govern session
synthesis apply:

1. the target vault is authorised by the room's actors, never by its text;
2. an unauthorised room is skipped entirely — a request without ``vault_id``
   is routed by BDH to its configured default, so "no scope" must mean "no
   synthesis", not "synthesis into whatever the server defaults to".
"""
import json
import sqlite3

import pytest

from room_synthesis_watcher import RoomSynthesisWatcher
from synthesis_scope import SynthesisPolicy


POLICY = SynthesisPolicy(
    profile_vaults={"client-a": "vault-a"},
    allow_room_registry=True,
    room_registry_path="unused.json",
)


def _room_db(path, room_id="r1", members=None, events=None):
    """Minimal hosted_rooms / hosted_room_events fixture."""
    db = sqlite3.connect(path)
    db.executescript("""
        create table hosted_rooms (room_id text primary key, name text,
            members_json text, updated_at real, disbanded_at real);
        create table hosted_room_events (room_id text, seq integer, kind text,
            actor_json text, payload_json text, created_at real);
    """)
    db.execute(
        "insert into hosted_rooms values (?,?,?,?,null)",
        (room_id, "Room", json.dumps(members or []), 1000.0),
    )
    for seq, (kind, actor, payload) in enumerate(events or [], start=1):
        db.execute(
            "insert into hosted_room_events values (?,?,?,?,?,?)",
            (room_id, seq, kind, json.dumps(actor), json.dumps(payload), 1000.0 + seq),
        )
    db.commit()
    db.close()


def _three_turns():
    return [
        ("message.user", {"id": "u"}, {"text": "how should the gate behave?"}),
        ("message.member", {"profile": "client-a"}, {"text": "by actor, not topic."}),
        ("message.user", {"id": "u"}, {"text": "what about a mixed room?"}),
        ("message.member", {"profile": "client-a"}, {"text": "not a client scope."}),
        ("message.user", {"id": "u"}, {"text": "and the default?"}),
        ("message.member", {"profile": "client-a"}, {"text": "skip it."}),
    ]


def _watcher(tmp_path, monkeypatch, members, events=None):
    db_path = tmp_path / "state.db"
    _room_db(db_path, members=members, events=events if events is not None else _three_turns())
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
    )


def test_all_bot_room_resolves_to_its_vault(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch,
                 members=[{"profile": "client-a"}, {"profile": "client-a-triage"}])
    assert w.resolve_vault("r1", "") == "vault-a"


def test_mixed_room_is_not_a_client_scope(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch,
                 members=[{"profile": "default"}, {"profile": "client-a"}])
    assert w.resolve_vault("r1", "") is None


def test_unauthorised_room_is_skipped_not_posted_without_a_vault(tmp_path, monkeypatch, capsys):
    """The flush must not happen at all when no vault is authorised."""
    w = _watcher(tmp_path, monkeypatch,
                 members=[{"profile": "default"}, {"profile": "client-a"}])
    posted = []
    monkeypatch.setattr(w, "_post_query", lambda payload: posted.append(payload))

    w._on_idle("r1")

    out = capsys.readouterr().out
    assert posted == [], "an unauthorised room must not be posted"
    assert "would POST" not in out, "no dry-run payload for an unauthorised room"


def test_authorised_room_payload_carries_vault_and_correlation(tmp_path, monkeypatch, capsys):
    w = _watcher(tmp_path, monkeypatch,
                 members=[{"profile": "client-a"}, {"profile": "client-a-triage"}])

    w._on_idle("r1")

    out = capsys.readouterr().out
    assert "would POST" in out, "authorised room must reach the flush"
    assert "vault='vault-a'" in out, "the authorised vault must be sent"


def test_payload_source_and_correlation_are_complete(tmp_path, monkeypatch):
    """Assert on the real payload builder, not only the dry-run string."""
    w = _watcher(tmp_path, monkeypatch,
                 members=[{"profile": "client-a"}])
    captured = {}

    def capture(payload):
        captured.update(payload)

    monkeypatch.setattr(w, "_post_query", capture)
    # dry_run off so the thread path is taken; _post_query is stubbed.
    w.dry_run = False
    monkeypatch.setattr("threading.Thread", lambda target, args, **k: type(
        "T", (), {"start": lambda self: target(*args)}
    )())

    w._on_idle("r1")

    assert captured["source"] == "room_synthesis"
    assert captured["vault_id"] == "vault-a"
    meta = captured["metadata"]
    assert meta["session_id"] == "r1"
    assert meta["room_id"] == "r1"
    assert len(meta["transcript_sha256"]) == 64
    assert meta["synthesis_id"]
    assert "USER:" in captured["user_prompt"]
