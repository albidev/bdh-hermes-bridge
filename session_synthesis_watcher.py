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
from synthesis_ledger import SynthesisLedger
from synthesis_scope import extract_mentions, load_policy, resolve_synthesis_vault

logger = logging.getLogger(__name__)

# A separate ledger file from the room watcher's. `SynthesisLedger` caches its
# contents at first load and rewrites the whole file on every record, so two
# PROCESSES sharing one file would overwrite each other's entries — the room
# watcher and this one are separate daemons, so they must not share.
LEDGER_ENV = "BDH_SESSION_SYNTHESIS_LEDGER_FILE"
DEFAULT_LEDGER = "bdh-session-synthesis-ledger.json"

# How many already-idle sessions a startup pass may submit. Bounded on purpose:
# each recovery is a synthesis on the local model (minutes), and recovery must
# never become an unbounded storm on a long-lived backlog.
DEFAULT_BACKLOG_LIMIT = 3

# A ceiling on how many candidates the backlog pass will even consider, so a
# machine with years of history does not read and hash thousands of transcripts
# on every start. Newest-first, so the cap keeps the useful end.
_BACKLOG_SCAN_LIMIT = 200

# The minimum-turn floor is OWNED by the bridge, not duplicated here. Both paths
# must agree: the standalone watcher serves TUI/Mission Control sessions and the
# bridge's in-process idle watcher serves the rest, so a literal here meant a
# configured BDH_SESSION_SYNTH_MIN_TURNS changed one path and silently left the
# other at 3 — the same "two resolvers, one decision" drift as the split home
# resolver. Read through the bridge; fall back only if it cannot be imported, so
# a missing bridge degrades the floor rather than the whole watcher.
_MIN_TURNS_FALLBACK = 1


def _caps() -> tuple[int, int]:
    """Return ``(user_max_chars, assistant_max_chars)`` from the bridge.

    The per-turn caps are owned by the bridge (``__init__._SESSION_TURN_*``) and
    read here so the two synthesis paths cannot drift. They previously differed:
    the standalone used a literal 1500 while the bridge was configured separately,
    so the same session produced a different transcript depending on which path
    picked it up. Falls back to the env vars (then to the bridge's own defaults)
    when the bridge is unimportable, so a plain-module launch degrades the source
    rather than the watcher.
    """
    try:
        import importlib

        bridge = importlib.import_module(
            __package__ + ".__init__" if __package__ else "__init__"
        )
        return (
            max(1, int(bridge._SESSION_TURN_USER_MAX_CHARS)),
            max(1, int(bridge._SESSION_TURN_ASSISTANT_MAX_CHARS)),
        )
    except Exception:
        try:
            return (
                max(1, int(os.environ.get("BDH_SESSION_TURN_USER_MAX_CHARS", "4000"))),
                max(1, int(os.environ.get("BDH_SESSION_TURN_ASSISTANT_MAX_CHARS", "12000"))),
            )
        except (TypeError, ValueError):
            return 4000, 12000


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
        ledger: "SynthesisLedger | None" = None,
        backlog_limit: int = DEFAULT_BACKLOG_LIMIT,
        dry_run: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.recover_session_id = recover_session_id
        self._recovery_seeded = False
        self.ledger = ledger
        self.backlog_limit = max(0, int(backlog_limit))
        self.dry_run = dry_run
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
        """Reconstruct one turn per user message; tool/system rows are dropped.

        An agentic turn is NOT a user/assistant pair. Between two user messages
        the assistant appears many times, interleaved with tool results:

            user -> assistant(finish=tool_calls, text="") -> tool -> assistant(...)
                 -> ... -> assistant(finish=stop, text=<the real answer>)

        Taking the FIRST assistant as the answer therefore captured an empty
        tool-call announcement and discarded the reply that arrives dozens of
        rows later. Observed on a 52-message session: the turn's actual answer
        was 9858 chars at row 39, and the transcript submitted was 479 chars
        containing neither of the two real answers — enough to make the
        extractor report "no concepts" on content that was full of them.

        So a turn accumulates assistant text until the next user message, and the
        transcript keeps the substance of the exchange rather than its opening
        line. ``finish_reason == "length"`` still marks a truncated response and
        is skipped.
        """
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
        pending_user: str | None = None
        parts: list[str] = []
        user_cap, assistant_cap = _caps()

        def _flush() -> None:
            if pending_user and parts:
                turns.append({
                    "user": pending_user[:user_cap],
                    "assistant": "\n".join(parts)[:assistant_cap],
                    "vault_id": None,
                    "context_only": True,
                })

        for role, content, finish_reason in rows:
            if role == "user":
                # A new user message closes the previous turn.
                _flush()
                pending_user = self._text(content)
                parts = []
            elif role == "assistant" and pending_user is not None:
                text = self._text(content)
                if str(finish_reason or "").lower() == "length":
                    # Truncated: the tail is missing, so the text is unreliable
                    # as an answer. Keep accumulating in case a later complete
                    # response arrives for the same turn.
                    continue
                if text:
                    parts.append(text)
        _flush()

        vault_id = self._resolve_recovery_vault(
            turns, session_id=session_id, db_paths=self.all_db_paths(),
        )
        if vault_id:
            for turn in turns:
                turn["vault_id"] = vault_id
        return turns[-200:]


    def _digest_for(self, turns: list[dict[str, Any]]) -> str | None:
        """Digest the transcript this session WOULD submit.

        Uses the bridge's own builder, so the digest matches byte for byte what
        actually goes on the wire. Re-deriving the shape here would let a
        formatting change silently break the ledger's dedupe.
        """
        if not turns:
            return None
        bridge = self._ensure_bridge()
        try:
            transcript, _, _ = bridge._build_session_transcript(turns)
        except Exception:
            return None
        if not transcript:
            return None
        import hashlib

        return hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    def _ensure_bridge(self):
        if self.bridge is None:
            import importlib

            self.bridge = importlib.import_module("__init__")
        return self.bridge

    def _eligible(self, session_id: str) -> list[dict[str, Any]] | None:
        """Turns for a session that may be synthesized, or None.

        Applies the same two gates as the idle path — the actor gate and the
        minimum-turn floor — plus the ledger, so a recovery pass can never submit
        something the live path would refuse, or repeat something already sent.
        """
        turns = self.rebuild_turns(session_id)
        if len(turns) < _min_turns():
            return None
        if not turns[0].get("vault_id"):
            return None
        if self.ledger is not None:
            digest = self._digest_for(turns)
            if digest and self.ledger.unchanged(session_id, digest):
                return None
        return turns

    def backlog(self, *, limit: int | None = None, now: float | None = None) -> list[str]:
        """Idle, authorised sessions whose content is not on the ledger.

        The transition-only trigger cannot reach a session whose live -> idle
        crossing happened while nobody was watching: either it went idle with
        nothing eligible (and stays idle forever), or it went idle while this
        process was not running (so it has no recorded state at all). In both, the
        content is lost in silence, indistinguishable from "nothing to learn".

        ``limit=None`` is unbounded; ``limit=0`` means nothing. Kept distinct: an
        overloaded ``0`` would make "disabled" and "unbounded" the same value.
        """
        current = time.time() if now is None else float(now)
        pending: list[tuple[float, str]] = []
        for session_id, last_activity in self.session_activity().items():
            if last_activity is None:
                continue
            if current - float(last_activity) < self.idle.threshold_seconds:
                continue  # still active: the transition trigger owns it
            pending.append((float(last_activity), session_id))
        pending.sort(reverse=True)
        candidates = [session_id for _, session_id in pending[:_BACKLOG_SCAN_LIMIT]]

        ready: list[str] = []
        for session_id in candidates:
            if self._eligible(session_id) is None:
                continue
            ready.append(session_id)
            if limit is not None and len(ready) >= max(0, int(limit)):
                break
        return ready if limit is None else ready[: max(0, int(limit))]

    def recover_backlog(self, *, limit: int | None = None, now: float | None = None) -> int:
        """Synthesize the backlog once. Returns how many sessions were handed off.

        Each goes through the same `_on_idle` as the live path, so the actor gate,
        the floor and the digest gate apply unchanged.
        """
        acted = 0
        for session_id in self.backlog(limit=limit, now=now):
            self._on_idle(session_id)
            acted += 1
        return acted

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
        # The idle trigger can fire more than once for the same content (a session
        # that goes quiet, is resumed, and goes quiet again without new turns).
        # The digest gate makes that idempotent instead of submitting twice.
        digest = self._digest_for(turns)
        if digest and self.ledger is not None and self.ledger.unchanged(session_id, digest):
            logger.info(
                "[synthesis-scope] session %s skipped — transcript already synthesized",
                session_id,
            )
            return
        bridge = self._ensure_bridge()
        if self.dry_run:
            print(
                f"[dry-run] session {session_id}: would POST source=session_synthesis "
                f"vault={turns[0].get('vault_id')!r} turns={len(turns)} "
                f"sha={(digest or '')[:12]}"
            )
            return
        with bridge._bdh_state_lock:
            bridge._session_buffers[session_id] = turns

        # Record only after BDH accepted: a digest written on a failed POST would
        # drop that transcript permanently, whereas a repeat in the dispatch
        # window is harmless (the synthesis id is deterministic over the content).
        def _record(sha: str) -> None:
            if self.ledger is not None:
                self.ledger.record(session_id, sha=sha)

        bridge._flush_session_synthesis(
            session_id, final=False, wait=True, on_success=_record,
        )

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
        # One bounded backlog pass at startup closes the blind spot described in
        # `backlog()`. It runs exactly once, and the ledger keeps it from
        # re-submitting on the next restart.
        if self.backlog_limit > 0:
            try:
                recovered = self.recover_backlog(limit=self.backlog_limit)
                if recovered:
                    print(f"[session-synthesis] startup backlog: {recovered} session(s) submitted")
            except Exception as exc:
                logger.warning("[session-synthesis] startup backlog failed: %s", exc)
        while True:
            self.scan_once()
            time.sleep(max(1.0, interval_seconds))


def _default_ledger_path(home: Path, override: str = "") -> Path:
    return Path(override or os.environ.get(LEDGER_ENV, "").strip() or home / DEFAULT_LEDGER)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--threshold", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--recover-session-id", default="")
    parser.add_argument("--ledger-path", default="")
    parser.add_argument(
        "--backlog-limit",
        type=int,
        default=DEFAULT_BACKLOG_LIMIT,
        help="sessions a startup backlog pass may submit (0 disables it)",
    )
    parser.add_argument(
        "--backlog-once",
        action="store_true",
        help="run the backlog pass and exit; does not start the idle loop",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    watcher = TranscriptIdleWatcher(
        db_path=args.db_path or home / "state.db",
        state_path=args.state_path or home / "bdh-session-synthesis-watcher.json",
        threshold_seconds=args.threshold,
        recover_session_id=args.recover_session_id or None,
        ledger=SynthesisLedger(_default_ledger_path(home, args.ledger_path)),
        backlog_limit=args.backlog_limit,
        dry_run=args.dry_run,
    )
    if args.backlog_once:
        print(f"backlog submitted: {watcher.recover_backlog(limit=args.backlog_limit)}")
    elif args.once:
        print(watcher.scan_once())
    else:
        watcher.run_forever(args.interval)


if __name__ == "__main__":
    main()
