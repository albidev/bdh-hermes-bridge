import json
import sqlite3

import pytest

from session_synthesis_watcher import TranscriptIdleWatcher
from synthesis_scope import SynthesisPolicy, resolve_synthesis_vault


def _db(path, session_id="s1", last_activity=1000.0, source="tui", profile_name=None,
        user_text="decision"):
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
        (session_id, "user", f"{user_text} one", "stop", 1),
        (session_id, "assistant", "answer one", "stop", 1),
        (session_id, "user", f"{user_text} two", "stop", 1),
        (session_id, "assistant", "answer two", "stop", 1),
        (session_id, "user", f"{user_text} three", "stop", 1),
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
    mention_prefixes={"client-a": "vault-a"},
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


def test_unknown_mention_iterator_cannot_fall_back_to_core():
    policy = SynthesisPolicy(
        allow_default_core_sessions=True,
        mention_prefixes={"client-a": "vault-a"},
        allow_room_registry=False,
    )
    assert resolve_synthesis_vault(
        session_profile="default",
        session_mentions=iter(["@unknown"]),
        trusted_default_session=True,
        policy=policy,
        registry={},
    ) is None


def test_serving_profile_does_not_authorise_a_one_to_one_session():
    """A serving profile is not an authorisation for a 1:1 session.

    A profile name says which agent RAN the turn, not what the work belongs to.
    A chat opened in a client profile to work on Hermes/BDH is served by that
    profile and would be filed into the client vault on every idle pass, with no
    residual signal distinguishing it from real client work. The profile stays
    authoritative inside a room (corroborated by the registry entry and the full
    membership) and is never a 1:1 fallback.
    """
    assert resolve_synthesis_vault(
        session_profile="client-a",
        policy=POLICY,
        registry={},
    ) is None
    assert resolve_synthesis_vault(
        session_profile="client-a-reviewer",
        policy=POLICY,
        registry={},
    ) is None


def test_addressed_actor_authorises_a_one_to_one_session():
    """The addressed actor is what authorises a 1:1 session."""
    assert resolve_synthesis_vault(
        session_profile="client-a",
        session_mentions=["@client-a"],
        policy=POLICY,
        registry={},
    ) == "vault-a"
    # The addressed actor also routes a client task run from an unrelated
    # profile, which is the case the serving-profile fallback used to miss.
    assert resolve_synthesis_vault(
        session_profile="default",
        session_mentions=["@client-a-reviewer"],
        policy=POLICY,
        registry={},
    ) == "vault-a"


def test_serving_profile_still_authorises_inside_a_room():
    """Inside a room the profile is corroborated, so it stays authoritative."""
    members = [{"profile": "client-a"}, {"profile": "client-a-triage"}]
    assert resolve_synthesis_vault(
        session_profile="client-a",
        room_id="r1",
        room_members=members,
        policy=POLICY,
        registry={"r1": "vault-a"},
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


def test_watcher_routes_opted_in_default_profile_session_to_core(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="default")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"version": 2, "allow_default_core_sessions": true, '
        '"allow_room_registry": false}', encoding="utf-8",
    )
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    assert [turn["vault_id"] for turn in watcher.rebuild_turns("s1")] == ["core"] * 3


@pytest.mark.parametrize("source,profile,user_text,expected", [
    ("tui", "default", "ask @client-a about", "vault-a"),
    ("tui", "default", "ask @unknown about", None),
    ("mission-control", "client-a", "decision", None),
    ("cron", "default", "decision", None),
])
def test_default_core_opt_in_never_overrides_actor_or_source(
    tmp_path, monkeypatch, source, profile, user_text, expected,
):
    db_path = tmp_path / "state.db"
    _db(db_path, source=source, profile_name=profile, user_text=user_text)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "allow_default_core_sessions": True,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    assert {turn["vault_id"] for turn in watcher.rebuild_turns("s1")} == {expected}


def test_default_core_opt_in_rejects_secondary_database(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    secondary = home / "profiles" / "other" / "state.db"
    secondary.parent.mkdir(parents=True)
    _db(home / "state.db", session_id="primary", profile_name="default")
    _db(secondary, session_id="secondary", profile_name="default")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text('{"allow_default_core_sessions": true}', encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=home / "state.db", state_path=tmp_path / "idle.json")

    assert {turn["vault_id"] for turn in watcher.rebuild_turns("primary")} == {"core"}
    assert {turn["vault_id"] for turn in watcher.rebuild_turns("secondary")} == {None}


def test_watcher_skips_a_profile_served_session_without_an_addressed_actor(tmp_path, monkeypatch):
    """A client profile is not what authorises a 1:1 session.

    The session below is served by the client profile and its transcript never
    names a client, but nothing distinguishes it from a chat opened in that
    profile to work on Hermes/BDH. It is skipped rather than filed into the
    client vault.
    """
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

    assert turns and all(turn["vault_id"] is None for turn in turns)


def test_watcher_routes_a_profile_served_session_by_its_addressed_actor(tmp_path, monkeypatch):
    """The addressed actor is what routes a 1:1 session, whoever served it."""
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="client-a", user_text="ask @client-a about")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"version": 1, "profile_vaults": {"client-a": "vault-a"},'
        ' "mention_prefixes": {"client-a": "vault-a"},'
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
    """A secondary profile's state.db is discovered and scanned.

    The routing expectation here is the actor gate's: the client-profile session
    is found and processed, but only its addressed actor would authorise a vault
    (neither session names one, so both are skipped rather than filed).
    """
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
    assert client_turns and all(t["vault_id"] is None for t in client_turns)
    default_turns = watcher.rebuild_turns("default-sess")
    assert default_turns and all(t["vault_id"] is None for t in default_turns)


def test_unauthorised_session_is_skipped_not_flushed_without_a_vault(tmp_path, monkeypatch):
    """A no-vault flush is not safe: BDH would route it to its own default.

    Skipping is the only fail-closed outcome, because the gate authorises by
    actor while a missing vault_id would let the transcript land in whatever
    vault the server defaults to.
    """
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="default")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(tmp_path / "absent.json"))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    flushed = []
    watcher.bridge = type("B", (), {
        "_bdh_state_lock": __import__("threading").RLock(),
        "_session_buffers": {},
        "_flush_session_synthesis": lambda *a, **k: flushed.append((a, k)),
    })()

    watcher._on_idle("s1")

    assert flushed == [], "an unauthorised session must not be flushed at all"
    assert watcher.bridge._session_buffers == {}


def test_room_turns_are_not_scanned_as_standalone_sessions(tmp_path, monkeypatch):
    """A room's per-member turns belong to the room watcher, not here.

    Scanning ``bot_room`` sessions in this watcher would synthesize the same
    conversation twice: once per member from a partial view, and once as the
    whole room with its aggregated transcript.
    """
    from session_synthesis_watcher import _PROFILE_SERVED_SOURCES

    assert "bot_room" not in _PROFILE_SERVED_SOURCES


def test_watcher_routes_an_addressed_actor_from_a_default_profile(tmp_path, monkeypatch):
    """The addressed actor outranks the runner profile.

    This is the case the profile rule cannot see: a client task run by the
    default profile. The user typed the bot handle, so the work is the bot's.
    """
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    db.execute("insert into sessions values ('s1','tui','default',1000.0,null)")
    rows = [
        ("s1", "user", "@client-a please check the deployment", "stop", 1),
        ("s1", "assistant", "On it.", "stop", 1),
        ("s1", "user", "any update?", "stop", 1),
        ("s1", "assistant", "Checking now.", "stop", 1),
        ("s1", "user", "thanks", "stop", 1),
        ("s1", "assistant", "Done.", "stop", 1),
    ]
    db.executemany("insert into messages(session_id,role,content,finish_reason,active)"
                   " values (?,?,?,?,?)", rows)
    db.commit(); db.close()

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "version": 2,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))

    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")
    turns = watcher.rebuild_turns("s1")

    assert turns and all(t["vault_id"] == "vault-a" for t in turns)


def test_watcher_ignores_prose_that_merely_names_a_client(tmp_path, monkeypatch):
    """Naming a client in prose is not addressing it.

    The whole point of the actor gate: this transcript is *about* a client but
    does not address anyone, so it must stay unscoped.
    """
    db_path = tmp_path / "state.db"
    _db(db_path, profile_name="default")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "version": 2,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")

    turns = watcher.rebuild_turns("s1")

    assert turns and all(t["vault_id"] is None for t in turns)


def test_watcher_authorises_every_bot_behind_the_prefix(tmp_path, monkeypatch):
    """A prefix mapping covers the whole bot family, not just the bare handle."""
    for handle in ("client-a", "client-a-triage", "client-a-reviewer"):
        db_path = tmp_path / f"{handle}.db"
        db = sqlite3.connect(db_path)
        db.executescript("""
            create table sessions (id text primary key, source text, profile_name text,
                                   last_activity_at real, ended_at real);
            create table messages (id integer primary key, session_id text, role text,
                                   content text, finish_reason text, active integer);
        """)
        db.execute("insert into sessions values ('s1','tui','default',1000.0,null)")
        db.executemany(
            "insert into messages(session_id,role,content,finish_reason,active) values (?,?,?,?,?)",
            [("s1", "user", f"@{handle} take a look", "stop", 1),
             ("s1", "assistant", "ok", "stop", 1),
             ("s1", "user", "and?", "stop", 1),
             ("s1", "assistant", "ok", "stop", 1),
             ("s1", "user", "done?", "stop", 1),
             ("s1", "assistant", "yes", "stop", 1)],
        )
        db.commit(); db.close()

        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps({
            "version": 2,
            "mention_prefixes": {"client-a": "vault-a"},
            "allow_room_registry": False,
        }), encoding="utf-8")
        monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
        w = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / f"{handle}-idle.json")
        turns = w.rebuild_turns("s1")
        assert turns and turns[0]["vault_id"] == "vault-a", handle


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


# ---------------------------------------------------------------------------
# The minimum-turn floor must be honoured AT THE CALL SITE, not just returned.
#
# Asserting on `_min_turns()` alone is not enough: it proves the helper reads the
# right value while the flush could still compare against a literal. That is the
# "correct gate, wrong caller" shape — the gate is right and the leak persists one
# layer down. These tests drive `_on_idle` with a real authorised session and
# assert on whether the flush actually happened.
# ---------------------------------------------------------------------------

def _authorised_one_turn_db(path, session_id="s1"):
    """One completed exchange, authorised via an addressed actor."""
    db = sqlite3.connect(path)
    db.executescript("""
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
    """)
    db.execute(
        "insert into sessions values (?, 'tui', 'default', 1000.0, null)",
        (session_id,),
    )
    db.executemany(
        "insert into messages(session_id,role,content,finish_reason,active) values (?,?,?,?,1)",
        [
            (session_id, "user", "@client-a one question", "stop"),
            (session_id, "assistant", "one answer", "stop"),
        ],
    )
    db.commit()
    db.close()


def _watcher_with_stub_bridge(db_path, tmp_path, monkeypatch):
    """A watcher whose bridge records flushes instead of posting them."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "version": 1,
        "mention_prefixes": {"client-a": "vault-a"},
        "profile_vaults": {"client-a": "vault-a"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy_file))
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")
    flushed = []
    watcher.bridge = type("B", (), {
        "_bdh_state_lock": __import__("threading").RLock(),
        "_session_buffers": {},
        "_flush_session_synthesis": lambda *a, **k: flushed.append(a),
    })()
    return watcher, flushed


def test_one_turn_session_is_flushed_at_the_default_floor(tmp_path, monkeypatch):
    """A single well-answered exchange must reach the flush.

    The floor used to be 3, so this session was silently dropped even though the
    actor authorisation resolved a vault. The per-turn write path that justified
    the higher floor is opt-in and unset, so nothing covered this session
    anywhere else.
    """
    db_path = tmp_path / "state.db"
    _authorised_one_turn_db(db_path)
    import __init__ as bridge_mod
    monkeypatch.setattr(bridge_mod, "_SESSION_SYNTH_MIN_TURNS", 1)
    watcher, flushed = _watcher_with_stub_bridge(db_path, tmp_path, monkeypatch)

    watcher._on_idle("s1")

    assert [call[-1] for call in flushed] == ["s1"], (
        "a one-turn authorised session must be flushed"
    )


def test_the_call_site_follows_the_floor_it_is_given(tmp_path, monkeypatch):
    """Below the configured floor the flush must NOT happen.

    This is what pins the CALL SITE: a literal `3` here would flush this session
    regardless of the configured floor, which is exactly the drift the single
    source of truth exists to prevent.
    """
    db_path = tmp_path / "state.db"
    _authorised_one_turn_db(db_path)
    import __init__ as bridge_mod
    monkeypatch.setattr(bridge_mod, "_SESSION_SYNTH_MIN_TURNS", 3)
    watcher, flushed = _watcher_with_stub_bridge(db_path, tmp_path, monkeypatch)

    watcher._on_idle("s1")

    assert flushed == [], "a 1-turn session must not flush when the floor is 3"


def test_the_same_session_flushes_when_the_floor_allows_it(tmp_path, monkeypatch):
    """Same session, same content: only the floor differs. Proves causality."""
    db_path = tmp_path / "state.db"
    _authorised_one_turn_db(db_path)
    import __init__ as bridge_mod

    monkeypatch.setattr(bridge_mod, "_SESSION_SYNTH_MIN_TURNS", 3)
    watcher_low, flushed_low = _watcher_with_stub_bridge(db_path, tmp_path, monkeypatch)
    watcher_low._on_idle("s1")

    monkeypatch.setattr(bridge_mod, "_SESSION_SYNTH_MIN_TURNS", 1)
    watcher_ok, flushed_ok = _watcher_with_stub_bridge(db_path, tmp_path, monkeypatch)
    watcher_ok._on_idle("s1")

    assert flushed_low == []
    assert [call[-1] for call in flushed_ok] == ["s1"]
