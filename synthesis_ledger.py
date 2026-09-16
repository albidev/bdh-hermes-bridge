"""Durable record of what each synthesis target has already been submitted.

Why this exists
---------------
Synthesis is expensive and writes into a vault, so re-submitting an unchanged
transcript is waste. More importantly, the idle trigger was *transition-only*: a
target that was already quiescent when the watcher started never went
live -> idle while being observed, so it could never be synthesized at all. No
amount of waiting fixes that — the state it has to leave is the state it is
already in.

This ledger makes eligibility a property of the CONTENT ("has this transcript
been synthesized?") rather than a property of an observed transition ("did I
watch it go idle?"). The transition remains the trigger; the ledger decides
whether the trigger is worth acting on, which is what lets a recovery pass
manufacture the transition for an already-idle target without re-submitting it
on every restart.

``last_synth_sha`` is the gate: the digest of the transcript last submitted.
``last_synth_seq`` records the highest source event included, so it is possible
to see which messages were already handled without recomputing a digest.

Written only after BDH accepted the request. A digest recorded on a failed POST
would drop that transcript permanently, whereas a duplicate in the window
between dispatch and the write is harmless: the synthesis id is deterministic
over (target, digest), so BDH folds the repeat into its duplicate accounting.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class SynthesisLedger:
    """Persistent last-submitted-digest record, keyed by synthesis target id."""

    VERSION = 1

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._loaded = False
        self._targets: dict[str, dict[str, Any]] = {}

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            # A missing or unreadable ledger means "nothing recorded yet", which
            # re-synthesizes once. That is recoverable; refusing to synthesize
            # because of a corrupt bookkeeping file is not.
            self._targets = {}
            return
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            self._targets = {}
            return
        targets = data.get("targets")
        if not isinstance(targets, dict):
            self._targets = {}
            return
        self._targets = {
            str(key): value
            for key, value in targets.items()
            if str(key)
            and isinstance(value, dict)
            and str(value.get("last_synth_sha") or "")
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": self.VERSION, "targets": self._targets},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # -- reads ---------------------------------------------------------------

    def sha_for(self, target_id: Any) -> str | None:
        with self._lock:
            self._load()
            entry = self._targets.get(str(target_id or "")) or {}
        return str(entry.get("last_synth_sha") or "") or None

    def seq_for(self, target_id: Any) -> int | None:
        with self._lock:
            self._load()
            entry = self._targets.get(str(target_id or "")) or {}
        value = entry.get("last_synth_seq")
        return int(value) if isinstance(value, int) else None

    def unchanged(self, target_id: Any, sha: Any) -> bool:
        """True when *sha* is the digest already submitted for *target_id*."""
        target_key = str(target_id or "")
        digest = str(sha or "")
        if not target_key or not digest:
            return False
        return self.sha_for(target_key) == digest

    # -- writes --------------------------------------------------------------

    def record(
        self,
        target_id: Any,
        *,
        sha: Any,
        seq: int | None = None,
        synthesis_id: Any = None,
        now: float | None = None,
    ) -> None:
        """Record a submitted digest. No-op when the identity is incomplete."""
        target_key = str(target_id or "")
        digest = str(sha or "")
        if not target_key or not digest:
            return
        entry: dict[str, Any] = {
            "last_synth_sha": digest,
            "synthesized_at": time.time() if now is None else float(now),
        }
        if seq is not None:
            try:
                entry["last_synth_seq"] = int(seq)
            except (TypeError, ValueError):
                pass
        if synthesis_id:
            entry["synthesis_id"] = str(synthesis_id)
        with self._lock:
            self._load()
            self._targets[target_key] = entry
            self._persist()
