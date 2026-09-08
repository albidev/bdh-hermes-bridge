"""Durable bridge-owned storage for eligible session synthesis buffers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps tests/imports usable.
    fcntl = None


class DurableSessionBuffer:
    """Atomic, profile-local buffer ledger; raw turns never enter the vault."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _locked(self):
        handle = self.lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    @staticmethod
    def _read_unlocked(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == DurableSessionBuffer.VERSION:
                sessions = data.get("sessions")
                if isinstance(sessions, dict):
                    return {"version": DurableSessionBuffer.VERSION, "sessions": sessions}
        except (OSError, ValueError, TypeError):
            pass
        return {"version": DurableSessionBuffer.VERSION, "sessions": {}}

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def append(self, session_id: str, turn: dict[str, Any], *, max_turns: int = 200) -> None:
        with self._locked():
            data = self._read_unlocked(self.path)
            sessions = data["sessions"]
            turns = list(sessions.get(session_id, {}).get("turns", []))
            turns.append(dict(turn))
            sessions[session_id] = {"turns": turns[-max_turns:]}
            self._write_unlocked(data)

    def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        with self._locked():
            data = self._read_unlocked(self.path)
            return [dict(turn) for turn in data["sessions"].get(session_id, {}).get("turns", [])]

    def all_sessions(self) -> dict[str, list[dict[str, Any]]]:
        with self._locked():
            data = self._read_unlocked(self.path)
            return {
                str(session_id): [dict(turn) for turn in record.get("turns", [])]
                for session_id, record in data["sessions"].items()
                if isinstance(record, dict) and isinstance(record.get("turns"), list)
            }

    def remove(self, session_id: str) -> None:
        with self._locked():
            data = self._read_unlocked(self.path)
            if session_id in data["sessions"]:
                data["sessions"].pop(session_id, None)
                self._write_unlocked(data)
