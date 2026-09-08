"""Standalone BDH session-synthesis idle watcher.

This process owns the TUI/Mission Control idle path without importing or
modifying Hermes core. It reads SessionDB read-only, reconstructs safe
user/assistant pairs, and invokes the bridge's existing Curate-gated flush.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from session_idle import SessionIdleWatcher


class TranscriptIdleWatcher:
    def __init__(
        self,
        *,
        db_path: str | os.PathLike[str],
        state_path: str | os.PathLike[str],
        threshold_seconds: float = 300.0,
        recover_session_id: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.recover_session_id = recover_session_id
        self._recovery_seeded = False
        self.bridge = None
        self.idle = SessionIdleWatcher(
            state_path,
            threshold_seconds=threshold_seconds,
            on_idle=self._on_idle,
        )

    def _db(self):
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0,
        )

    def session_activity(self) -> dict[str, float | None]:
        try:
            with self._db() as db:
                rows = db.execute(
                    "SELECT id, last_activity_at FROM sessions "
                    "WHERE ended_at IS NULL AND source IN ('tui', 'mission-control')"
                ).fetchall()
            return {str(session_id): activity for session_id, activity in rows}
        except sqlite3.Error:
            return {}

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in value
            ).strip()
        return str(value or "").strip()

    @staticmethod
    def _resolve_recovered_vault(turns: list[dict[str, Any]]) -> str | None:
        query = "\n".join(str(turn.get("user") or "") for turn in turns).strip()
        if not query:
            return None
        try:
            from vault_router import suggest_vault
            return suggest_vault(query)
        except (ImportError, OSError, ValueError, TypeError):
            return None

    def rebuild_turns(self, session_id: str) -> list[dict[str, Any]]:
        """Reconstruct conservative pairs; tool/system rows are never buffered."""
        try:
            with self._db() as db:
                rows = db.execute(
                    "SELECT role, content, finish_reason FROM messages "
                    "WHERE session_id = ? AND active = 1 "
                    "ORDER BY id",
                    (session_id,),
                ).fetchall()
        except sqlite3.Error:
            return []
        turns: list[dict[str, Any]] = []
        pending_user = None
        for role, content, finish_reason in rows:
            if role == "user":
                pending_user = self._text(content)
            elif role == "assistant" and pending_user:
                if str(finish_reason or "").lower() != "length":
                    turns.append({
                        "user": pending_user[:1500],
                        "assistant": self._text(content)[:1500],
                        "vault_id": None,
                        "context_only": True,
                    })
                pending_user = None
        vault_id = self._resolve_recovered_vault(turns)
        if vault_id:
            for turn in turns:
                turn["vault_id"] = vault_id
        return turns[-200:]

    def _on_idle(self, session_id: str) -> None:
        turns = self.rebuild_turns(session_id)
        if len(turns) < 3:
            return
        if self.bridge is None:
            import importlib
            self.bridge = importlib.import_module("__init__")
        with self.bridge._bdh_state_lock:
            self.bridge._session_buffers[session_id] = turns
        self.bridge._flush_session_synthesis(session_id, final=False, wait=True)

    def scan_once(self, *, now: float | None = None) -> int:
        activity = self.session_activity()
        if self.recover_session_id and not self._recovery_seeded:
            self.idle.mark_live(self.recover_session_id)
            self._recovery_seeded = True
        # A session that is active when the watcher starts establishes the
        # baseline; already-old sessions remain silent unless explicitly selected
        # through --recover-session-id.
        return self.idle.scan(activity, now=now)

    def run_forever(self, interval_seconds: float = 60.0) -> None:
        while True:
            self.scan_once()
            time.sleep(max(1.0, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--threshold", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--recover-session-id", default="")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    watcher = TranscriptIdleWatcher(
        db_path=args.db_path or home / "state.db",
        state_path=args.state_path or home / "bdh-session-synthesis-watcher.json",
        threshold_seconds=args.threshold,
        recover_session_id=args.recover_session_id or None,
    )
    if args.once:
        print(watcher.scan_once())
    else:
        watcher.run_forever(args.interval)


if __name__ == "__main__":
    main()
