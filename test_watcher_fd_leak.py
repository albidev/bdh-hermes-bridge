"""The watchers must not leak file descriptors on their repeated read paths.

`with sqlite3.connect(...)` is a TRANSMISSION context manager, not a resource
one: it commits/rolls back on exit and leaves the connection OPEN. Both watchers
wrap every read in it and never call `close()`, so descriptors accumulate on
state.db / -wal / -shm until Python's GC happens to reclaim them.

Measured on the live daemon: the session watcher oscillated between 141 and 221
open descriptors against launchd's `maxfiles` limit of 256. One busier window
exhausts it and the watcher stops being able to read its own database — it keeps
running and silently does nothing.

These tests count descriptors on the database file across many read cycles, so
they fail on an accumulating implementation and pass on a bounded one.
"""
from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from room_synthesis_watcher import RoomSynthesisWatcher  # noqa: E402
from session_synthesis_watcher import TranscriptIdleWatcher  # noqa: E402


def _open_fds_on(path) -> int:
    """Descriptors this process holds on `path`, via lsof.

    `/dev/fd` readlink does not resolve SQLite's descriptors on macOS, and the
    in-process alternatives do not see them either, so the descriptor table is
    read from the outside. The binary is addressed absolutely: the test suite
    runs under a minimal PATH.

    Note the leak is *bounded by the garbage collector* — SQLite connections are
    reclaimed when their reference dies, so a tight loop in a test may look
    clean while a long-lived daemon accumulates between collections. The counts
    here are therefore a lower bound; the real evidence is the live daemon.
    """
    lsof = next(
        (c for c in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(c)), None
    )
    if lsof is None:
        pytest.skip("lsof not available")
    out = subprocess.run(
        [lsof, "-p", str(os.getpid())], capture_output=True, text=True
    ).stdout
    return len([line for line in out.splitlines() if str(path) in line])


def _session_db(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table sessions (id text primary key, source text, profile_name text,
                               last_activity_at real, ended_at real);
        create table messages (id integer primary key, session_id text, role text,
                               content text, finish_reason text, active integer);
        """
    )
    for i in range(5):
        con.execute(
            "insert into sessions values (?,?,?,?,null)",
            (f"s{i}", "tui", "default", 1000.0),
        )
        con.execute(
            "insert into messages(session_id,role,content,finish_reason,active) "
            "values (?,?,?,?,1)",
            (f"s{i}", "user", f"question {i}", "stop"),
        )
    con.commit()
    con.close()
    return db


def test_session_read_paths_do_not_accumulate_descriptors(tmp_path):
    """Repeated session reads must not grow the descriptor count.

    The cyclic collector is disabled for the duration: SQLite connections form
    reference cycles (cursors point back at the connection), so they are not
    freed by refcounting and only disappear when a collection happens to run.
    That is why the live daemon oscillates rather than growing monotonically —
    the leak is real but bounded by GC timing, which is not a guarantee. Holding
    GC still makes the accumulation deterministic instead of flaky.
    """
    db = _session_db(tmp_path)
    watcher = TranscriptIdleWatcher(db_path=db, state_path=tmp_path / "idle.json")

    watcher.session_activity()  # warm up
    gc.collect()
    before = _open_fds_on(db)

    gc.disable()
    try:
        for _ in range(30):
            watcher.session_activity()
            watcher._locate_session("s0", watcher.all_db_paths())
            watcher.rebuild_turns("s0")
        after = _open_fds_on(db)
    finally:
        gc.enable()
        gc.collect()

    growth = after - before
    assert growth <= 2, (
        f"descriptor count grew by {growth} over 30 read cycles "
        f"({before} -> {after}) with the collector paused: the connection is "
        f"relying on garbage collection instead of being closed"
    )


def test_session_read_paths_release_descriptors_after_reads(tmp_path):
    """Descriptors must be released without waiting for a collection."""
    db = _session_db(tmp_path)
    watcher = TranscriptIdleWatcher(db_path=db, state_path=tmp_path / "idle.json")
    watcher.session_activity()

    gc.disable()
    try:
        for _ in range(5):
            watcher.session_activity()
        held = _open_fds_on(db)
    finally:
        gc.enable()

    assert held <= 2, (
        f"{held} descriptors are still held right after the reads: closing the "
        f"connection must not depend on the garbage collector"
    )


def test_room_read_paths_do_not_accumulate_descriptors(tmp_path):
    """Repeated room reads must not grow the descriptor count.

    The cyclic collector is paused for the same reason as the session case:
    without it the accumulation is masked by whichever collection happens to
    run, which is exactly why the live daemon oscillates instead of growing.
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table hosted_rooms (room_id text primary key, members_json text,
                                   updated_at real, disbanded_at real);
        create table hosted_room_events (room_id text, kind text, actor_json text,
                                         payload_json text, created_at real, seq integer);
        """
    )
    con.execute(
        "insert into hosted_rooms values (?,?,?,null)",
        ("r1", '[{"handle": "client-a", "profile": "client-a"}]', 1000.0),
    )
    con.commit()
    con.close()

    watcher = RoomSynthesisWatcher(
        db_path=db, state_path=tmp_path / "idle.json", registry_path=tmp_path / "reg.json"
    )

    watcher.room_activity()  # warm up
    gc.collect()
    before = _open_fds_on(db)

    gc.disable()
    try:
        for _ in range(30):
            watcher.room_activity()
            watcher.room_members("r1")
            watcher.rebuild_turns("r1")
        after = _open_fds_on(db)
    finally:
        gc.enable()
        gc.collect()

    growth = after - before
    assert growth <= 2, (
        f"descriptor count grew by {growth} over 30 read cycles "
        f"({before} -> {after}) with the collector paused: the connection is "
        f"relying on garbage collection instead of being closed"
    )


def test_room_watcher_honours_bdh_api_url(monkeypatch):
    """The room watcher must resolve BDH_API_URL, like the rest of the bridge.

    Its launchd plist sets BDH_API_URL while the watcher read only the CLI flag,
    so the variable was dead config. The practical failure: pointing BDH_API_URL
    at a stub redirected the session watcher but left the room watcher posting
    to the real server on 8643.
    """
    import room_synthesis_watcher as rsw

    monkeypatch.setenv("BDH_API_URL", "http://stub.example:9999")
    assert rsw._default_bdh_url() == "http://stub.example:9999"

    # An empty or blank value falls back to the built-in default.
    monkeypatch.setenv("BDH_API_URL", "   ")
    assert rsw._default_bdh_url() == rsw.DEFAULT_BDH_URL
    monkeypatch.delenv("BDH_API_URL", raising=False)
    assert rsw._default_bdh_url() == rsw.DEFAULT_BDH_URL
