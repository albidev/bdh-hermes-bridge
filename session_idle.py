"""Bridge-owned idle watcher for session synthesis.

This module deliberately lives in bdh-hermes-bridge instead of Hermes core. It
observes the durable SessionDB activity clock only for sessions that already
have bridge synthesis state, then invokes a bridge callback on a live -> idle
transition. The ledger survives plugin-process restarts and is independent of
Mission Control.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Mapping


class SessionIdleWatcher:
    """Persistent, transition-only idle detector for bridge-owned session buffers."""

    VERSION = 1

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        threshold_seconds: float = 300.0,
        on_idle: Callable[[str], None],
    ) -> None:
        self.state_path = Path(state_path)
        self.threshold_seconds = max(30.0, float(threshold_seconds))
        self.on_idle = on_idle
        self._lock = threading.RLock()
        self._loaded = False
        self._states: dict[str, str] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == self.VERSION:
                states = data.get("sessions", {})
                if isinstance(states, dict):
                    self._states = {
                        str(key): value
                        for key, value in states.items()
                        if value in {"live", "idle"}
                    }
        except (OSError, ValueError, TypeError):
            self._states = {}

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"version": self.VERSION, "sessions": self._states}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.state_path)

    def mark_live(self, session_id: str) -> None:
        session_id = str(session_id or "")
        if not session_id:
            return
        with self._lock:
            self._load()
            if self._states.get(session_id) == "live":
                return
            self._states[session_id] = "live"
            self._persist()

    def scan(
        self,
        activity_by_session: Mapping[str, float | None],
        *,
        now: float | None = None,
    ) -> int:
        """Emit once for buffered sessions that crossed the idle threshold."""
        now = time.time() if now is None else float(now)
        emitted: list[str] = []
        with self._lock:
            self._load()
            changed = False
            for raw_session_id, raw_last_activity in activity_by_session.items():
                session_id = str(raw_session_id or "")
                if not session_id or raw_last_activity is None:
                    continue
                try:
                    last_activity = float(raw_last_activity)
                except (TypeError, ValueError):
                    continue
                if now - last_activity < self.threshold_seconds:
                    if self._states.get(session_id) != "live":
                        self._states[session_id] = "live"
                        changed = True
                    continue
                if self._states.get(session_id) == "live":
                    self._states[session_id] = "idle"
                    emitted.append(session_id)
                    changed = True
            if changed:
                self._persist()
        for session_id in emitted:
            self.on_idle(session_id)
        return len(emitted)

    def start(
        self,
        activity_provider: Callable[[set[str]], Mapping[str, float | None]],
        session_ids_provider: Callable[[], set[str]],
        *,
        interval_seconds: float = 60.0,
    ) -> threading.Event:
        """Start a daemon poller and return its stop event."""
        stop = threading.Event()
        interval = max(1.0, float(interval_seconds))

        def loop() -> None:
            while not stop.wait(interval):
                session_ids = session_ids_provider()
                if not session_ids:
                    continue
                try:
                    self.scan(activity_provider(session_ids))
                except Exception:
                    # The bridge must never take down the agent runtime because
                    # SessionDB is busy or temporarily unavailable.
                    continue

        threading.Thread(target=loop, name="bdh-session-idle-watcher", daemon=True).start()
        return stop
