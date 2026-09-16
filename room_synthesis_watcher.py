"""Standalone BDH room-synthesis idle watcher.

Parallel to session_synthesis_watcher.py for hosted group rooms. This process
owns the room idleness -> curated synthesis path for Mission Control group
rooms without importing or modifying Hermes core OR the bridge plugin.

Room sources (state.db, read-only):
  hosted_rooms       -> room identity, members, disbanded_at, updated_at
  hosted_room_events -> message.user / message.member / turn.settled

Design decisions (user-confirmed, 2026-09-12):
  - NO automatic flush on disband: only the idle transition triggers synthesis.
  - source name: "room_synthesis" (separate from "session_synthesis").
  - Disbanded rooms are excluded entirely: a closed room is never synthesized
    by this watcher; only active rooms that cross the idle threshold are.

Vault resolution order:
  Authorised by the room's ACTORS, never by its transcript text. The room
  registry entry (set in the room creation form) and the member profiles
  decide; a room with any default participant is not a client scope at all.
  An unauthorised room is skipped entirely — a request without vault_id is
  routed by BDH to its configured default.

Re-synthesis is gated by CONTENT, not by an observed transition:
  `synthesis_ledger.py` records the digest last submitted per room. The idle
  transition is the trigger; the ledger decides whether the trigger is worth
  acting on. Without it, a room already quiescent when the watcher starts can
  never be synthesized (it never crosses live -> idle while observed), and a
  restart with no ledger would re-submit everything on every pass.

The synthesis request mirrors _bdh_query_async semantics: POST /api/query
with user_prompt=transcript, source=room_synthesis, vault_id and metadata.
The BDH graph harness owns candidate staging; this script never writes to
vaults or candidate directories on its own.

Usage:
  python3 room_synthesis_watcher.py [--db-path ...] [--state-path ...]
      [--threshold 300] [--interval 60] [--registry ...] [--once]
      [--ledger-path ...] [--backlog-limit N] [--backlog-once] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from session_idle import SessionIdleWatcher
from synthesis_ledger import SynthesisLedger
from synthesis_scope import resolve_synthesis_vault

logger = logging.getLogger(__name__)

DEFAULT_BDH_URL = "http://127.0.0.1:8643"
DEFAULT_MIN_TURNS = 3
DEFAULT_MAX_CHARS = 6000
DEFAULT_TIMEOUT = 300.0
DEFAULT_THRESHOLD = 300.0
DEFAULT_INTERVAL = 60.0
DEFAULT_REGISTRY = "~/Projects/hermes-mission-control/server/room_vaults.json"
LEDGER_ENV = "BDH_SYNTHESIS_LEDGER_FILE"
DEFAULT_LEDGER = "bdh-synthesis-ledger.json"
# How many already-idle rooms a startup pass may submit. Bounded on purpose:
# recovery must never turn into an unbounded synthesis storm on a large backlog.
DEFAULT_BACKLOG_LIMIT = 3

_KIND_USER = "message.user"
_KIND_MEMBER = "message.member"
_SYNTHESIS_QUERY = (
    "Synthesis of an entire group chat room. Extract and record any durable "
    "concept, decision, architecture choice, lesson learned, or reusable insight "
    "that emerged across the room as a whole — not per-message noise. Ignore "
    "operational status, diagnostics, and transient tasks."
)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in value
        ).strip()
    return str(value or "").strip()


def _parse_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _load_vault_registry(path: str | os.PathLike[str]) -> dict[str, str]:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, ValueError):
        pass
    return {}


class RoomSynthesisWatcher:
    def __init__(
        self,
        *,
        db_path: str | os.PathLike[str],
        state_path: str | os.PathLike[str],
        registry_path: str | os.PathLike[str] = DEFAULT_REGISTRY,
        bdh_url: str = DEFAULT_BDH_URL,
        threshold_seconds: float = DEFAULT_THRESHOLD,
        min_turns: int = DEFAULT_MIN_TURNS,
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout: float = DEFAULT_TIMEOUT,
        dry_run: bool = False,
        ledger: SynthesisLedger | None = None,
        backlog_limit: int = DEFAULT_BACKLOG_LIMIT,
    ) -> None:
        self.db_path = Path(db_path)
        self.registry = _load_vault_registry(registry_path)
        self.bdh_url = bdh_url.rstrip("/")
        self.min_turns = min_turns
        self.max_chars = max_chars
        self.timeout = timeout
        self.dry_run = dry_run
        self.ledger = ledger
        self.backlog_limit = max(0, int(backlog_limit))
        self.idle = SessionIdleWatcher(
            state_path,
            threshold_seconds=threshold_seconds,
            on_idle=self._on_idle,
        )

    # -- activity / turn reconstruction ------------------------------------

    def _db(self) -> sqlite3.Connection:
        """Open a read-only connection.

        Callers must CLOSE it: `with sqlite3.connect(...)` is a transaction
        context manager and leaves the connection open, so descriptors
        accumulate until the cyclic collector runs. Use
        `contextlib.closing(self._db())` for every read.
        """
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0,
        )

    def room_activity(self) -> dict[str, float | None]:
        """Map room_id -> last event time for ACTIVE (non-disbanded) rooms."""
        try:
            with closing(self._db()) as db:
                rows = db.execute(
                    "SELECT h.room_id, "
                    "       COALESCE((SELECT MAX(e.created_at) FROM hosted_room_events e "
                    "                 WHERE e.room_id = h.room_id), h.updated_at) AS last_at "
                    "FROM hosted_rooms h "
                    "WHERE h.disbanded_at IS NULL"
                ).fetchall()
            return {str(rid): float(last_at) for rid, last_at in rows if last_at is not None}
        except sqlite3.Error:
            return {}

    def room_members(self, room_id: str) -> list[dict[str, str]]:
        try:
            with closing(self._db()) as db:
                row = db.execute(
                    "SELECT members_json FROM hosted_rooms WHERE room_id = ?",
                    (room_id,),
                ).fetchone()
        except sqlite3.Error:
            return []
        if not row or not row[0]:
            return []
        try:
            members = json.loads(row[0])
            if not isinstance(members, list):
                return []
        except (ValueError, TypeError):
            return []
        return [
            {
                "handle": str(m.get("handle") or m.get("profile") or ""),
                "profile": str(m.get("profile") or ""),
            }
            for m in members
            if isinstance(m, dict)
        ]

    def resolve_vault(self, room_id: str, transcript: str) -> str | None:
        """Authorise the room's synthesis vault from its actors, not its text.

        The transcript is accepted for call-site compatibility but is never
        inspected: a room that merely discusses a client must not be written
        into that client's vault. Authorisation comes from the room registry
        entry and the member profiles, and a room with any default
        (non-authorised) participant is not a client scope at all.

        The previous member-profile branch returned the raw profile name,
        which is not necessarily a vault id, and fell back to the same
        textual router that caused cross-vault contamination.
        """
        return resolve_synthesis_vault(
            session_profile=None,
            room_id=room_id,
            room_members=self.room_members(room_id),
            registry=self.registry,
        )

    def rebuild_turns(self, room_id: str) -> list[dict[str, Any]]:
        """Reconstruct user/member turns from room events.

        Rooms have a single user writer; member replies may be absent when
        the driver is paused, stopped, or errored. A turn is therefore every
        user message, with any member replies accumulated until the next user
        message appended as the assistant part.
        """
        try:
            with closing(self._db()) as db:
                rows = db.execute(
                    "SELECT kind, actor_json, payload_json, created_at "
                    "FROM hosted_room_events "
                    "WHERE room_id = ? AND kind IN (?, ?) "
                    "ORDER BY seq",
                    (room_id, _KIND_USER, _KIND_MEMBER),
                ).fetchall()
        except sqlite3.Error:
            return []
        turns: list[dict[str, Any]] = []
        for kind, actor_json, payload_json, _created in rows:
            if kind == _KIND_USER:
                turns.append(self._turn(_text(_parse_json(payload_json).get("text")), ""))
            else:
                actor = _parse_json(actor_json)
                profile = str(actor.get("profile") or actor.get("id") or "member")
                text = _text(_parse_json(payload_json).get("text"))
                if text and turns:
                    prev = turns[-1]
                    if not prev["assistant"]:
                        prev["assistant"] = f"[{profile}] {text}"
                    else:
                        prev["assistant"] += f"\n[{profile}] {text}"
        return [t for t in turns if t["user"]][-200:]

    @staticmethod
    def _turn(user: str, assistant: str) -> dict[str, Any]:
        return {
            "user": user[:1500],
            "assistant": assistant[:1500],
            "vault_id": None,
            "context_only": False,
        }

    def _last_source_seq(self, room_id: str) -> int | None:
        """Highest ``seq`` of a user/member event, for the ledger cursor.

        ``seq`` is monotonic per room (``PRIMARY KEY (room_id, seq)``) and
        indexed, so it is comparable across passes: it records WHICH messages
        were handled without re-deriving a digest.
        """
        try:
            with closing(self._db()) as db:
                row = db.execute(
                    "SELECT MAX(seq) FROM hosted_room_events "
                    "WHERE room_id = ? AND kind IN (?, ?)",
                    (room_id, _KIND_USER, _KIND_MEMBER),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def transcript_for(self, room_id: str) -> tuple[str, int, int | None]:
        """Return ``(transcript, turn_count, last_source_seq)`` for a room.

        One place builds the transcript, so the idle path and the backlog pass
        cannot disagree about what a room's content is — they would otherwise
        derive different digests for the same room and defeat the ledger.
        """
        turns = self.rebuild_turns(room_id)
        if len(turns) < self.min_turns:
            return "", len(turns), None
        transcript = "\n".join(
            f"USER: {t['user']}" + (f"\nASSISTANT: {t['assistant']}" if t["assistant"] else "")
            for t in turns
        )
        if len(transcript) > self.max_chars:
            transcript = transcript[-self.max_chars:]
        if not transcript.strip():
            return "", len(turns), None
        return transcript, len(turns), self._last_source_seq(room_id)

    @staticmethod
    def _digest(transcript: str) -> str:
        return hashlib.sha256(transcript.encode("utf-8")).hexdigest()

    def backlog(self, *, limit: int | None = None, now: float | None = None) -> list[str]:
        """Active, authorised, idle rooms whose content is not on the ledger.

        A room already quiescent when the watcher starts never crosses
        live -> idle while being observed, so the transition-only trigger can
        never fire for it: the state it must leave is the state it is already
        in. This enumerates the resulting blind spot explicitly instead of
        leaving it silent, and returns newest-first so a bounded pass keeps the
        useful end of the backlog.

        ``limit=None`` means unbounded; ``limit=0`` means "nothing". The two are
        kept distinct on purpose — an overloaded ``0`` would make "disabled" and
        "unbounded" the same value, which is how a safety bound silently becomes
        no bound at all.
        """
        current = time.time() if now is None else float(now)
        activity = self.room_activity()
        pending: list[tuple[float, str]] = []
        for room_id, last_activity in activity.items():
            if last_activity is None:
                continue
            if current - float(last_activity) < self.idle.threshold_seconds:
                continue  # still hot: the transition trigger will handle it
            transcript, _, _ = self.transcript_for(room_id)
            if not transcript:
                continue
            if self.ledger is not None and self.ledger.unchanged(room_id, self._digest(transcript)):
                continue  # already submitted — this is the dedupe that matters
            if self.resolve_vault(room_id, transcript) is None:
                continue  # unauthorised rooms are never synthesized
            pending.append((float(last_activity), room_id))
        pending.sort(reverse=True)
        rooms = [room_id for _, room_id in pending]
        if limit is None:
            return rooms
        return rooms[:max(0, int(limit))]

    def recover_backlog(self, *, limit: int | None = None, now: float | None = None) -> int:
        """Synthesize the backlog once. Returns how many rooms were handed off.

        Each room goes through the SAME flush as the idle path, so the vault
        gate and the digest gate apply unchanged. This is what makes recovering
        an already-idle room safe: the ledger, not the pass, prevents repeats.
        """
        acted = 0
        for room_id in self.backlog(limit=limit, now=now):
            self._on_idle(room_id)
            acted += 1
        return acted

    # -- flush ---------------------------------------------------------------

    def _on_idle(self, room_id: str) -> None:
        transcript, turn_count, source_seq = self.transcript_for(room_id)
        if not transcript:
            return
        transcript_sha256 = self._digest(transcript)
        # Content gate: a target whose transcript digest is already on record has
        # nothing new to learn. This is checked here, inside the flush, so every
        # caller inherits it — the idle path, the backlog pass, and any future
        # one.
        if self.ledger is not None and self.ledger.unchanged(room_id, transcript_sha256):
            logger.info(
                "[room-synthesis] room %s skipped — transcript already synthesized",
                room_id,
            )
            return
        vault_id = self.resolve_vault(room_id, transcript)
        # Fail-closed: BDH routes a request without vault_id to its configured
        # default, so posting anyway would land an unauthorised room transcript
        # in a vault. Skipping is the only safe outcome.
        if not vault_id:
            logger.info("[room-synthesis] room %s skipped — no authorised vault", room_id)
            return
        metadata = {
            "synthesis_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"bdh-room-synthesis:v1:{room_id}:{transcript_sha256}",
                )
            ),
            "session_id": room_id,
            "room_id": room_id,
            "queued_at": time.time(),
            "transcript_sha256": transcript_sha256,
            "accepted_count": turn_count,
        }
        if source_seq is not None:
            metadata["source_seq"] = source_seq
        payload = {
            "query": _SYNTHESIS_QUERY,
            "user_prompt": transcript,
            "source": "room_synthesis",
            "vault_id": vault_id,
            "metadata": metadata,
        }
        if self.dry_run:
            if self.ledger is not None:
                self.ledger.record(
                    room_id,
                    sha=transcript_sha256,
                    seq=source_seq,
                    synthesis_id=metadata["synthesis_id"],
                )
            print(
                f"[dry-run] room {room_id}: would POST {self.bdh_url}/api/query "
                f"source=room_synthesis vault={vault_id!r} turns={turn_count} "
                f"chars={len(transcript)} sha={transcript_sha256[:12]} "
                f"seq={source_seq}"
            )
            return
        threading.Thread(
            target=self._post_query,
            args=(payload,),
            daemon=True,
            name=f"room-synthesis-{room_id[:12]}",
        ).start()

    def _post_query(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        meta = payload.get("metadata", {}) or {}
        room_id = str(meta.get("room_id") or "")
        req = urllib.request.Request(
            f"{self.bdh_url}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8") or "{}")
        except Exception as exc:
            # Deliberately NOT recorded: a digest written on a failed POST would
            # drop that transcript for good. A repeat in the dispatch window is
            # harmless because the synthesis id is deterministic over
            # (room_id, digest) and BDH folds it into its duplicate accounting.
            print(f"[room-synthesis] POST failed: {exc}")
            return
        # The digest and cursor travel in the payload, so the ledger is updated
        # from the SAME values that were submitted rather than from parallel
        # arguments that could drift apart.
        if self.ledger is not None and meta.get("transcript_sha256"):
            self.ledger.record(
                room_id,
                sha=meta.get("transcript_sha256"),
                seq=meta.get("source_seq"),
                synthesis_id=meta.get("synthesis_id"),
            )
        new = result.get("new_concepts", [])
        activated = len(result.get("activated_notes", []))
        print(
            f"[room-synthesis] {room_id or '?'}: "
            f"new_concepts={len(new)} activated={activated}"
        )

    # -- loop ----------------------------------------------------------------

    def scan_once(self, *, now: float | None = None) -> int:
        activity = self.room_activity()
        return self.idle.scan(activity, now=now)

    def run_forever(self, interval_seconds: float = DEFAULT_INTERVAL) -> None:
        # One bounded backlog pass at startup closes the blind spot described in
        # `backlog()`: rooms that went quiescent before this process existed can
        # never produce the observed transition. It runs exactly once, and the
        # ledger keeps it from re-submitting on the next restart.
        if self.backlog_limit > 0:
            try:
                recovered = self.recover_backlog(limit=self.backlog_limit)
                if recovered:
                    print(f"[room-synthesis] startup backlog: {recovered} room(s) submitted")
            except Exception as exc:
                logger.warning("[room-synthesis] startup backlog failed: %s", exc)
        while True:
            self.scan_once()
            time.sleep(max(1.0, interval_seconds))


def _default_bdh_url() -> str:
    """The BDH endpoint, honouring BDH_API_URL like the rest of the bridge.

    The launchd plist sets BDH_API_URL, and the session watcher resolves it
    through the bridge package — this watcher read only the CLI flag, so the
    environment variable was dead config and a test that pointed BDH_API_URL at
    a stub would still POST to the real server.
    """
    return os.environ.get("BDH_API_URL", "").strip() or DEFAULT_BDH_URL


def main() -> None:
    parser = argparse.ArgumentParser(description="Room idle synthesis watcher")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--bdh-url", default=_default_bdh_url())

    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-turns", type=int, default=DEFAULT_MIN_TURNS)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--ledger-path", default="")
    parser.add_argument(
        "--backlog-limit",
        type=int,
        default=DEFAULT_BACKLOG_LIMIT,
        help="rooms a startup backlog pass may submit (0 disables it)",
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
    ledger_path = (
        args.ledger_path
        or os.environ.get(LEDGER_ENV, "").strip()
        or home / DEFAULT_LEDGER
    )
    watcher = RoomSynthesisWatcher(
        db_path=args.db_path or home / "state.db",
        state_path=args.state_path or home / "bdh-room-synthesis-watcher.json",
        registry_path=args.registry,
        bdh_url=args.bdh_url,
        threshold_seconds=args.threshold,
        min_turns=args.min_turns,
        max_chars=args.max_chars,
        dry_run=args.dry_run,
        ledger=SynthesisLedger(ledger_path),
        backlog_limit=args.backlog_limit,
    )
    if args.backlog_once:
        print(f"backlog submitted: {watcher.recover_backlog(limit=args.backlog_limit)}")
    elif args.once:
        print(f"rooms scanned: {watcher.scan_once()}")
    else:
        watcher.run_forever(args.interval)


if __name__ == "__main__":
    main()
