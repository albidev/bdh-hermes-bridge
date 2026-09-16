"""Standalone BDH session-synthesis idle watcher.

This process owns the TUI/Mission Control idle path without importing or
modifying Hermes core. It reads SessionDB read-only, reconstructs safe
user/assistant pairs, and invokes the bridge's existing Curate-gated flush.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from session_idle import SessionIdleWatcher
from synthesis_scope import extract_mentions, load_policy, resolve_synthesis_vault

logger = logging.getLogger(__name__)

# The minimum-turn floor is OWNED by the bridge, not duplicated here. Both paths
# must agree: the standalone watcher serves TUI/Mission Control sessions and the
# bridge's in-process idle watcher serves the rest, so a literal here meant a
# configured BDH_SESSION_SYNTH_MIN_TURNS changed one path and silently left the
# other at 3 — the same "two resolvers, one decision" drift as the split home
# resolver. Read through the bridge; fall back only if it cannot be imported, so
# a missing bridge degrades the floor rather than the whole watcher.
_MIN_TURNS_FALLBACK = 1


def _min_turns() -> int:
    """Return the shared minimum-turn floor (bridge-owned, ``>= 1``)."""
    try:
        import importlib

        bridge = importlib.import_module(
            __package__ + ".__init__" if __package__ else "__init__"
        )
        return max(1, int(bridge._SESSION_SYNTH_MIN_TURNS))
    except Exception:
        # A plugin loader may run this file as a plain module with no package,
        # and the bridge reads env at import time. Falling back to the env var
        # keeps a configured value honoured in that mode too.
        try:
            return max(1, int(os.environ.get("BDH_SESSION_SYNTH_MIN_TURNS", _MIN_TURNS_FALLBACK)))
        except (TypeError, ValueError):
            return _MIN_TURNS_FALLBACK

# Session sources served by a Hermes profile rather than by a user terminal.
# ``bot_room`` is deliberately excluded: those sessions are the per-member turns
# *inside* a hosted room, and the room watcher already synthesizes the room as a
# whole from its aggregated transcript. Scanning them here would synthesize the
# same conversation twice, from a partial view.
_PROFILE_SERVED_SOURCES = ("tui", "mission-control")


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
        # Profile-scoped databases of the same Hermes home. A session served by
        # a secondary profile is stored in that profile's own state.db, so a
        # watcher pinned to the default database would never see it.
        self.extra_db_paths = self._discover_profile_dbs(self.db_path)
        self.bridge = None
        self.idle = SessionIdleWatcher(
            state_path,
            threshold_seconds=threshold_seconds,
            on_idle=self._on_idle,
        )

    @staticmethod
    def _discover_profile_dbs(db_path: Path) -> list[Path]:
        profiles_root = db_path.parent / "profiles"
        if not profiles_root.is_dir():
            return []
        found: list[Path] = []
        for entry in sorted(profiles_root.iterdir()):
            candidate = entry / "state.db"
            try:
                if candidate.is_file() and candidate != db_path:
                    found.append(candidate)
            except OSError:
                continue
        return found

    def all_db_paths(self) -> list[Path]:
        return [self.db_path, *self.extra_db_paths]

    def _db(self, db_path: Path | None = None):
        """Open a read-only connection.

        Callers must CLOSE it. `with sqlite3.connect(...)` is a transaction
        context manager: it does not close the connection, and SQLite
        connections form reference cycles, so they are only reclaimed when the
        cyclic collector happens to run. A long-lived watcher therefore
        accumulates descriptors between collections — measured at 141-221 held
        against launchd's 256 maxfiles limit. Use `contextlib.closing(self._db())`
        (or an explicit close() in a finally) for every read.
        """
        target = db_path or self.db_path
        return sqlite3.connect(
            f"file:{target}?mode=ro", uri=True, timeout=1.0,
        )

    def session_activity(self) -> dict[str, float | None]:
        activity: dict[str, float | None] = {}
        for db_path in self.all_db_paths():
            rows: list[Any] = []
            try:
                with closing(self._db(db_path)) as db:
                    rows = db.execute(
                        "SELECT id, last_activity_at FROM sessions "
                        "WHERE ended_at IS NULL AND source IN "
                        f"({','.join('?' * len(_PROFILE_SERVED_SOURCES))})",
                        _PROFILE_SERVED_SOURCES,
                    ).fetchall()
            except sqlite3.Error:
                continue
            for session_id, last_activity in rows:
                activity[str(session_id)] = last_activity
        return activity


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
    def _locate_session(session_id: str, db_paths: list[Path]) -> tuple[Path | None, str | None]:
        """Return (db_path, serving profile_name) for a session, if known."""
        for db_path in db_paths:
            try:
                with closing(sqlite3.connect(
                    f"file:{db_path}?mode=ro", uri=True, timeout=1.0,
                )) as db:
                    row = db.execute(
                        "SELECT profile_name FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
            except sqlite3.Error:
                continue
            if row is not None:
                return db_path, (str(row[0]).strip() if row[0] else None)
        return None, None

    @classmethod
    def _resolve_recovery_vault(
        cls,
        turns: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        db_paths: list[Path] | None = None,
    ) -> str | None:
        """Authorise a synthesis vault from actors, never from prose.

        Two actor signals are read. The *addressed* actor is the ``@handle``
        tokens the user typed: they name who was asked to do the work, which is
        independent of the profile that served the run. The *serving* profile is
        the fallback. Neither inspects the transcript prose, so a session that
        merely talks about a client cannot be routed into that client's vault.
        """
        profile_name = None
        if session_id and db_paths:
            _, profile_name = cls._locate_session(session_id, db_paths)
        return resolve_synthesis_vault(
            session_profile=profile_name,
            session_mentions=cls._mentions_from_turns(turns),
            policy=load_policy(),
        )

    @staticmethod
    def _mentions_from_turns(turns: list[dict[str, Any]]) -> list[str]:
        """Collect ``@handle`` tokens addressed in the session's user messages."""
        return extract_mentions(*[t.get("user") for t in turns])

    def rebuild_turns(self, session_id: str) -> list[dict[str, Any]]:
        """Reconstruct conservative pairs; tool/system rows are never buffered."""
        db_path, _ = self._locate_session(session_id, self.all_db_paths())
        target_db = db_path or self.db_path
        try:
            with closing(self._db(target_db)) as db:
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
        vault_id = self._resolve_recovery_vault(
            turns, session_id=session_id, db_paths=self.all_db_paths(),
        )
        if vault_id:
            for turn in turns:
                turn["vault_id"] = vault_id
        return turns[-200:]


    def _on_idle(self, session_id: str) -> None:
        turns = self.rebuild_turns(session_id)
        if len(turns) < _min_turns():
            return
        # The actor gate is fail-closed: an unauthorised session is skipped, not
        # flushed with no vault. A no-vault flush would be routed by BDH to its
        # configured default, which would let un-authorised transcripts land in
        # a vault — the leak this gate exists to stop.
        if not turns[0].get("vault_id"):
            logger.info(
                "[synthesis-scope] session %s skipped — no authorised vault", session_id
            )
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
