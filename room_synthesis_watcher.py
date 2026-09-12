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
  1. Mission Control room_vaults.json registry (explicit, set in the room
     creation form);
  2. member profile fallback (non-default member profile -> vault id);
  3. bridge vault_router semantic suggestion (if importable);
  4. core.

The synthesis request mirrors _bdh_query_async semantics: POST /api/query
with user_prompt=transcript, source=room_synthesis, vault_id and metadata.
The BDH graph harness owns candidate staging; this script never writes to
vaults or candidate directories on its own.

Usage:
  python3 room_synthesis_watcher.py [--db-path ...] [--state-path ...]
      [--threshold 300] [--interval 60] [--registry ...] [--once]
      [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from session_idle import SessionIdleWatcher

DEFAULT_BDH_URL = "http://127.0.0.1:8643"
DEFAULT_MIN_TURNS = 3
DEFAULT_MAX_CHARS = 6000
DEFAULT_TIMEOUT = 300.0
DEFAULT_THRESHOLD = 300.0
DEFAULT_INTERVAL = 60.0
DEFAULT_REGISTRY = "~/Projects/hermes-mission-control/server/room_vaults.json"

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
    ) -> None:
        self.db_path = Path(db_path)
        self.registry = _load_vault_registry(registry_path)
        self.bdh_url = bdh_url.rstrip("/")
        self.min_turns = min_turns
        self.max_chars = max_chars
        self.timeout = timeout
        self.dry_run = dry_run
        self.idle = SessionIdleWatcher(
            state_path,
            threshold_seconds=threshold_seconds,
            on_idle=self._on_idle,
        )

    # -- activity / turn reconstruction ------------------------------------

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0,
        )

    def room_activity(self) -> dict[str, float | None]:
        """Map room_id -> last event time for ACTIVE (non-disbanded) rooms."""
        try:
            with self._db() as db:
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
            with self._db() as db:
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
        explicit = (self.registry.get(room_id) or "").strip()
        if explicit:
            return explicit
        for member in self.room_members(room_id):
            profile = member.get("profile", "").strip()
            if profile and profile != "default":
                return profile
        try:
            from vault_router import suggest_vault
            suggested = suggest_vault(transcript[:2000])
            if suggested:
                return str(suggested)
        except (ImportError, OSError, ValueError, TypeError):
            pass
        return None

    def rebuild_turns(self, room_id: str) -> list[dict[str, Any]]:
        """Reconstruct user/member turns from room events.

        Rooms have a single user writer; member replies may be absent when
        the driver is paused, stopped, or errored. A turn is therefore every
        user message, with any member replies accumulated until the next user
        message appended as the assistant part.
        """
        try:
            with self._db() as db:
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

    # -- flush ---------------------------------------------------------------

    def _on_idle(self, room_id: str) -> None:
        turns = self.rebuild_turns(room_id)
        if len(turns) < self.min_turns:
            return
        transcript = "\n".join(
            f"USER: {t['user']}" + (f"\nASSISTANT: {t['assistant']}" if t["assistant"] else "")
            for t in turns
        )
        if len(transcript) > self.max_chars:
            transcript = transcript[-self.max_chars:]
        if not transcript.strip():
            return
        transcript_sha256 = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        vault_id = self.resolve_vault(room_id, transcript)
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
            "accepted_count": len(turns),
        }
        payload = {
            "query": _SYNTHESIS_QUERY,
            "user_prompt": transcript,
            "source": "room_synthesis",
            "metadata": metadata,
        }
        if vault_id:
            payload["vault_id"] = vault_id
        if self.dry_run:
            print(
                f"[dry-run] room {room_id}: would POST {self.bdh_url}/api/query "
                f"source=room_synthesis vault={vault_id!r} turns={len(turns)} "
                f"chars={len(transcript)} sha={transcript_sha256[:12]}"
            )
            return
        threading.Thread(
            target=self._post_query, args=(payload,), daemon=True,
            name=f"room-synthesis-{room_id[:12]}",
        ).start()

    def _post_query(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.bdh_url}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8") or "{}")
            new = result.get("new_concepts", [])
            activated = len(result.get("activated_notes", []))
            print(
                f"[room-synthesis] {payload.get('metadata', {}).get('room_id', '?')}: "
                f"new_concepts={len(new)} activated={activated}"
            )
        except Exception as exc:
            print(f"[room-synthesis] POST failed: {exc}")

    # -- loop ----------------------------------------------------------------

    def scan_once(self, *, now: float | None = None) -> int:
        activity = self.room_activity()
        return self.idle.scan(activity, now=now)

    def run_forever(self, interval_seconds: float = DEFAULT_INTERVAL) -> None:
        while True:
            self.scan_once()
            time.sleep(max(1.0, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Room idle synthesis watcher")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--state-path", default="")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--bdh-url", default=DEFAULT_BDH_URL)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--min-turns", type=int, default=DEFAULT_MIN_TURNS)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    watcher = RoomSynthesisWatcher(
        db_path=args.db_path or home / "state.db",
        state_path=args.state_path or home / "bdh-room-synthesis-watcher.json",
        registry_path=args.registry,
        bdh_url=args.bdh_url,
        threshold_seconds=args.threshold,
        min_turns=args.min_turns,
        max_chars=args.max_chars,
        dry_run=args.dry_run,
    )
    if args.once:
        print(f"rooms scanned: {watcher.scan_once()}")
    else:
        watcher.run_forever(args.interval)


if __name__ == "__main__":
    main()
