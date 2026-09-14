import sqlite3

import pytest

from session_synthesis_watcher import TranscriptIdleWatcher
from synthesis_scope import SynthesisPolicy, resolve_synthesis_vault


def _db(path, session_id="s1", last_activity=1000.0, source="tui", profile_name=None):
    db = sqlite3.connect(path)
    db.executescript("""
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    db.execute(
        "insert into sessions values (?, ?, ?, ?, null)",
        (session_id, source, profile_name, last_activity),
    )
    rows = [
        (session_id, "user", "decision one", "stop", 1),
        (session_id, "assistant", "answer one", "stop", 1),
        (session_id, "user", "decision two", "stop", 1),
        (session_id, "assistant", "answer two", "stop", 1),
        (session_id, "user", "decision three", "stop", 1),
        (session_id, "assistant", "answer three", "stop", 1),
    ]
    db.executemany(
        "insert into messages(session_id,role,content,finish_reason,active) values (?,?,?,?,?)",
        rows,
    )
    db.commit(); db.close()


# ---------------------------------------------------------------------------
# Gate invariants: authorisation comes from the actor, never from the topic.
# ---------------------------------------------------------------------------

POLICY = SynthesisPolicy(
    profile_vaults={"client-a": "vault-a"},
    allow_room_registry=True,
    room_registry_path="unused.json",
)


def test_default_profile_topic_is_never_routed_to_the_client_vault():
    """A default-profile session that talks about a client stays unscoped."""
    assert resolve_synthesis_vault(
        session_profile="default",
        policy=POLICY,
        registry={},
    ) is None


def test_serving_profile_authorises_its_vault():
    assert resolve_synthesis_vault(
        session_profile="client-a",
        policy=POLICY,
        registry={},
    ) == "vault-a"
    assert resolve_synthesis_vault(
        session_profile="client-a-reviewer",
        policy=POLICY,
        registry={},
    ) == "vault-a"


def test_unlisted_profile_is_not_authorised():
    assert resolve_synthesis_vault(
        session_profile="bdhsynthesis",
        policy=POLICY,
        registry={},
    ) is None


def test_missing_policy_means_no_synthesis(tmp_path, monkeypatch):
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(tmp_path / "absent.json"))
    assert resolve_synthesis_vault(session_profile="client-a") is None


def test_mixed_room_is_not_a_client_scope():
    members = [{"profile": "default"}, {"profile": "client-a"}]
    assert resolve_synthesis_vault(
        session_profile=None,
        room_id="r1",
        room_members=members,
        policy=POLICY,
        registry={},
    ) is None


def test_unanimous_room_members_authorise_the_vault():
    members = [{"profile": "client-a"}, {"profile": "client-a-triage"}]
    assert resolve_synthesis_vault(
        session_profile=None,
        room_id="r1",
        room_members=members,
        policy=POLICY,
        registry={},
    ) == "vault-a"


def test_registry_entry_is_used_when_the_serving_profile_is_unauthorised():
    members = [{"profile": "client-a"}]
    assert resolve_synthesis_vault(
        session_profile="default",
        room_id="r1",
        room_members=members,
        policy=POLICY,
        registry={"r1": "vault-a"},
    ) == "vault-a"


def test_registry_cannot_override_a_contradicting_member_profile():
    members = [{"profile": "default"}, {"profile": "client-a"}]
    assert resolve_synthesis_vault(
        session_profile="default",
        room_id="r1",
        room_members=members,
        policy=POLICY,
        registry={"r1": "vault-a"},
    ) is None


def test_registry_is_ignored_when_the_policy_disables_it():
    policy = SynthesisPolicy(
        profile_vaults={"client-a": "vault-a"},
        allow_room_registry=False,
        room_registry_path="",
    )
    assert resolve_synthesis_vault(
        session_profile=None,
        room_id="r1",
        room_members=[],
        policy=policy,
        registry={"r1": "vault-a"},
    ) is None


# ---------------------------------------------------------------------------
# Watcher behaviour
# ---------------------------------------------------------------------------

def test_watcher_rebuilds_conservative_pairs(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"; _db(db_path)
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(tmp_path / "absent.json"))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")
    assert watcher.rebuild_turns("s1") == [
        {"user": "decision one", "assistant": "answer one", "vault_id": None, "context_only": True},
        {"user": "decision two", "assistant": "answer two", "vault_id": None, "context_only": True},
        {"user": "decision three", "assistant": "answer three", "vault_id": None, "context_only": True},
    ]


def test_watcher_uses_the_serving_profile_not_the_transcript(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="client-a")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"version": 1, "profile_vaults": {"client-a": "vault-a"},'
        ' "allow_room_registry": false}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    turns = watcher.rebuild_turns("s1")

    assert all(turn["vault_id"] == "vault-a" for turn in turns)


def test_watcher_skips_a_default_profile_session_mentioning_a_client(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="default")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"version": 1, "profile_vaults": {"client-a": "vault-a"},'
        ' "allow_room_registry": false}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    turns = watcher.rebuild_turns("s1")

    assert turns and all(turn["vault_id"] is None for turn in turns)


def test_watcher_reads_secondary_profile_databases(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "vault-a").mkdir(parents=True)
    _db(home / "state.db", session_id="default-sess", profile_name="default")
    _db(
        home / "profiles" / "vault-a" / "state.db",
        session_id="client-sess",
        profile_name="client-a",
    )
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"version": 1, "profile_vaults": {"client-a": "vault-a"},'
        ' "allow_room_registry": false}',
        encoding="utf-8",
    )
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=home / "state.db", state_path=tmp_path / "idle.json")

    assert set(watcher.session_activity()) == {"default-sess", "client-sess"}
    client_turns = watcher.rebuild_turns("client-sess")
    assert client_turns and all(t["vault_id"] == "vault-a" for t in client_turns)
    default_turns = watcher.rebuild_turns("default-sess")
    assert default_turns and all(t["vault_id"] is None for t in default_turns)


def test_recovery_target_emits_once_after_idle(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"; _db(db_path)
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(tmp_path / "absent.json"))
    watcher = TranscriptIdleWatcher(
        db_path=db_path,
        state_path=tmp_path / "idle.json",
        recover_session_id="s1",
    )
    events = []
    watcher.idle.on_idle = events.append
    assert watcher.scan_once(now=1400.0) == 1
    assert events == ["s1"]
    assert watcher.scan_once(now=1500.0) == 0
