import json
import logging
import sqlite3
from pathlib import Path

from session_synthesis_watcher import TranscriptIdleWatcher
from synthesis_ledger import SynthesisLedger


def _valid_empty_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            create table sessions (
                id text primary key,
                source text,
                profile_name text,
                last_activity_at real,
                ended_at real
            );
            create table messages (
                id integer primary key,
                session_id text,
                role text,
                content text,
                finish_reason text,
                active integer
            );
        """)


def _watcher(tmp_path, monkeypatch, *, db_path=None, ledger=None):
    policy = tmp_path / "synthesis-policy.json"
    policy.write_text(json.dumps({
        "version": 1,
        "mention_prefixes": {"asterion": "thomas-vault"},
        "allow_room_registry": False,
    }), encoding="utf-8")
    monkeypatch.setenv("BDH_SYNTHESIS_POLICY_FILE", str(policy))
    return TranscriptIdleWatcher(
        db_path=db_path or tmp_path / "state.db",
        state_path=tmp_path / "watcher-state.json",
        ledger=ledger,
        backlog_limit=0,
    )


def test_preflight_accepts_empty_readable_db_and_checks_exact_actor_route_without_writes(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    ledger_path = tmp_path / "ledger.json"
    watcher = _watcher(
        tmp_path, monkeypatch, db_path=db_path, ledger=SynthesisLedger(ledger_path),
    )

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is True
    assert report["databases"] == [{
        "name": "state.db", "status": "ok", "open_sessions": 0,
    }]
    assert report["authorization"] == {
        "status": "authorized",
        "mode": "actor_handle",
        "expected_vault_id": "thomas-vault",
        "resolved_vault_id": "thomas-vault",
    }
    assert not (tmp_path / "watcher-state.json").exists()
    assert not ledger_path.exists()


def test_preflight_distinguishes_missing_database_from_empty_workload(tmp_path, monkeypatch):
    watcher = _watcher(tmp_path, monkeypatch)

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["databases"] == [{
        "name": "state.db", "status": "missing", "open_sessions": None,
    }]
    assert any("database_missing" in error for error in report["errors"])


def test_preflight_distinguishes_invalid_database(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["databases"][0]["name"] == "state.db"
    assert report["databases"][0]["status"] == "invalid_or_unreadable"
    assert report["databases"][0]["open_sessions"] is None
    assert report["databases"][0]["detail"]
    assert any("database_invalid_or_unreadable" in error for error in report["errors"])


def test_preflight_rejects_unmapped_actor_without_guessing_a_vault(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)

    report = watcher.preflight(actor_handle="unknown", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["authorization"]["status"] == "unauthorized"
    assert report["authorization"]["resolved_vault_id"] is None
    assert any("authorization_unresolved" in error for error in report["errors"])


def test_preflight_reports_missing_policy_separately(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)
    (tmp_path / "synthesis-policy.json").unlink()

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["policy"]["status"] == "missing"
    assert "policy_missing" in report["errors"]


def test_preflight_reports_invalid_policy_separately(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)
    (tmp_path / "synthesis-policy.json").write_text("{bad json", encoding="utf-8")

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["policy"]["status"] == "invalid"
    assert "policy_invalid" in report["errors"]


def test_preflight_reports_semantically_invalid_policy_without_crashing(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)
    (tmp_path / "synthesis-policy.json").write_text(
        json.dumps({"version": "not-an-integer", "mention_prefixes": {"asterion": "thomas-vault"}}),
        encoding="utf-8",
    )

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is False
    assert report["policy"]["status"] == "invalid"
    assert "policy_invalid" in report["errors"]


def test_preflight_default_route_only_accepts_literal_core(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)
    policy_path = tmp_path / "synthesis-policy.json"
    policy_path.write_text(
        '{"version": 1, "allow_default_core_sessions": true, '
        '"allow_room_registry": false}', encoding="utf-8",
    )

    report = watcher.preflight(default_core=True, expected_vault_id="core")

    assert report["ok"] is True
    assert report["authorization"]["resolved_vault_id"] == "core"


def test_cli_check_is_read_only_and_reports_exact_resolution(tmp_path, monkeypatch, capsys):
    from session_synthesis_watcher import main

    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    exit_code = main([
        "--db-path", str(db_path),
        "--check",
        "--check-actor-handle", "asterion",
        "--expect-vault-id", "thomas-vault",
    ])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["authorization"]["resolved_vault_id"] == "thomas-vault"
    assert report["databases"][0]["open_sessions"] == 0
    assert not (tmp_path / "watcher-state.json").exists()
    assert not (tmp_path / "bdh-session-synthesis-ledger.json").exists()


def test_session_activity_logs_database_failures_without_transcript_content(
    tmp_path, monkeypatch, caplog
):
    db_path = tmp_path / "state.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    watcher = _watcher(tmp_path, monkeypatch, db_path=db_path)

    with caplog.at_level(logging.WARNING):
        assert watcher.session_activity() == {}

    assert "session database read failed" in caplog.text
    assert "invalid_or_unreadable" in caplog.text
    assert "state.db" in caplog.text
    assert "not sqlite" not in caplog.text


def test_preflight_counts_open_desktop_sessions_without_writing_state(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _valid_empty_db(db_path)
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT INTO sessions VALUES (?, ?, 'default', 1000.0, ?)",
            [
                ("desktop-open", "desktop", None),
                ("desktop-ended", "desktop", 1200.0),
                ("room", "bot_room", None),
                ("cron", "cron", None),
            ],
        )
    before = db_path.read_bytes()
    ledger_path = tmp_path / "ledger.json"
    watcher = _watcher(tmp_path, monkeypatch, ledger=SynthesisLedger(ledger_path))

    report = watcher.preflight(actor_handle="asterion", expected_vault_id="thomas-vault")

    assert report["ok"] is True
    assert report["databases"][0]["open_sessions"] == 1
    assert not (tmp_path / "watcher-state.json").exists()
    assert not ledger_path.exists()
    assert db_path.read_bytes() == before
