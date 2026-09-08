import sqlite3

from session_synthesis_watcher import TranscriptIdleWatcher


def _db(path, session_id="s1", last_activity=1000.0):
    db = sqlite3.connect(path)
    db.executescript("""
        create table sessions (id text primary key, source text, last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text, content text, finish_reason text, active integer);
    """)
    db.execute("insert into sessions values (?, 'tui', ?, null)", (session_id, last_activity))
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


def test_watcher_rebuilds_conservative_pairs(tmp_path):
    db_path = tmp_path / "state.db"; _db(db_path)
    watcher = TranscriptIdleWatcher(db_path=db_path, state_path=tmp_path / "idle.json")
    assert watcher.rebuild_turns("s1") == [
        {"user": "decision one", "assistant": "answer one", "vault_id": None, "context_only": True},
        {"user": "decision two", "assistant": "answer two", "vault_id": None, "context_only": True},
        {"user": "decision three", "assistant": "answer three", "vault_id": None, "context_only": True},
    ]


def test_recovery_target_emits_once_after_idle(tmp_path):
    db_path = tmp_path / "state.db"; _db(db_path)
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
