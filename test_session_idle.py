from session_idle import SessionIdleWatcher


def test_idle_watcher_persists_transition_across_restart(tmp_path):
    events = []
    path = tmp_path / "bdh-session-idle.json"
    watcher = SessionIdleWatcher(path, threshold_seconds=300, on_idle=events.append)
    watcher.mark_live("session-1")

    assert watcher.scan({"session-1": 1000.0}, now=1299.0) == 0
    assert watcher.scan({"session-1": 1000.0}, now=1300.0) == 1
    assert events == ["session-1"]
    assert watcher.scan({"session-1": 1000.0}, now=1400.0) == 0

    restarted = SessionIdleWatcher(path, threshold_seconds=300, on_idle=events.append)
    assert restarted.scan({"session-1": 1000.0}, now=1500.0) == 0

    restarted.mark_live("session-1")
    assert restarted.scan({"session-1": 1800.0}, now=2100.0) == 1
    assert events == ["session-1", "session-1"]


def test_idle_watcher_does_not_emit_for_unknown_already_idle_session(tmp_path):
    events = []
    watcher = SessionIdleWatcher(
        tmp_path / "state.json", threshold_seconds=300, on_idle=events.append,
    )

    assert watcher.scan({"old-session": 1000.0}, now=2000.0) == 0
    assert events == []
