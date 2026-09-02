"""Experimental semantic vault-routing overlay for bdh-hermes-bridge.

This module implements a *local* routing index that can suggest a vault_id
from the user query when the deterministic resolver returns None.
It is intentionally additive: if deterministic routing already resolved a
vault, this overlay is skipped. If it cannot confidently suggest a vault,
it returns None and the existing behavior continues unchanged.

Index source
------------
A local JSON/YAML index file with entries like:
  - vault_id: core
    title: "Core"
    concepts: ["BDH routing", "vault", "scope", "alias"]
  - vault_id: crossnection
    title: "Crossnection"
    concepts: ["Inspector", "Pentair", "valvola", "automazione"]

The index file path is controlled by ``BDH_VAULT_ROUTER_INDEX`` and must
remain local/untracked. If missing or invalid, the overlay is disabled.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence


logger = logging.getLogger(__name__)

_DEFAULT_INDEX_PATH = "vault-router-index.local.json"
_MIN_CONFIDENCE = 0.4
_MAX_SUGGESTIONS = 3


class VaultRouterError(Exception):
    """Non-fatal router error; the hook should continue without routing."""


class _IndexEntry:
    def __init__(self, vault_id: str, title: str, concepts: Sequence[str]) -> None:
        self.vault_id = vault_id.strip()
        self.title = title.strip()
        self.concepts = [c.strip() for c in concepts if isinstance(c, str) and c.strip()]

    @property
    def text(self) -> str:
        parts = [self.title, self.vault_id]
        parts.extend(self.concepts)
        return " ".join(parts)


def _load_index() -> List[_IndexEntry]:
    raw_path = os.environ.get("BDH_VAULT_ROUTER_INDEX", "").strip() or _DEFAULT_INDEX_PATH
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise VaultRouterError(f"vault router index missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise VaultRouterError(f"invalid vault router index: {exc}") from exc

    if not isinstance(data, list):
        raise VaultRouterError("vault router index must be a list")

    entries: List[_IndexEntry] = []
    seen: set[tuple[str, str]] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        vault_id = str(item.get("vault_id") or item.get("vault") or "").strip()
        title = str(item.get("title", "")).strip()
        concepts = item.get("concepts", [])
        if not vault_id or not title:
            continue
        key = (vault_id, title)
        if key in seen:
            continue
        seen.add(key)
        entries.append(_IndexEntry(vault_id=vault_id, title=title, concepts=concepts))
    return entries


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _score(entry: _IndexEntry, normalized_query: str) -> float:
    if not entry.concepts:
        return 0.0
    title_hit = _normalize(entry.title) in normalized_query
    vault_hit = _normalize(entry.vault_id) in normalized_query
    concept_hits = sum(1 for concept in entry.concepts if _normalize(concept) in normalized_query)
    if title_hit or vault_hit:
        return min(1.0, 0.55 + concept_hits * 0.15)
    if concept_hits:
        return min(1.0, 0.2 + concept_hits * 0.15)
    return 0.0


def suggest_vault(query: str) -> Optional[str]:
    """Return a suggested vault_id from local index, or None."""
    try:
        entries = _load_index()
    except VaultRouterError as exc:
        logger.debug("[vault-router] disabled: %s", exc)
        return None
    if not entries:
        logger.debug("[vault-router] empty index")
        return None
    normalized_query = _normalize(query)
    scored = sorted(
        ((entry, _score(entry, normalized_query)) for entry in entries),
        key=lambda item: item[1],
        reverse=True,
    )
    scored = [(entry, score) for entry, score in scored if score > 0.0][:_MAX_SUGGESTIONS]
    if len(scored) < 1:
        logger.debug("[vault-router] no matches")
        return None
    best_entry, best_score = scored[0]
    if len(scored) == 1:
        if best_score >= _MIN_CONFIDENCE:
            logger.info("[vault-router] unique suggestion=%s score=%.3f", best_entry.vault_id, best_score)
            return best_entry.vault_id
        logger.debug("[vault-router] single match below threshold: %s %.3f", best_entry.vault_id, best_score)
        return None

    # Group top matches by vault and pick the strongest vault by best score.
    best_by_vault: dict[str, float] = {}
    best_entry_by_vault: dict[str, object] = {}
    for entry, score in scored:
        if best_by_vault.get(entry.vault_id, -1.0) < score:
            best_by_vault[entry.vault_id] = score
            best_entry_by_vault[entry.vault_id] = entry

    ranked_vaults = sorted(best_by_vault.items(), key=lambda item: item[1], reverse=True)
    if len(ranked_vaults) == 1:
        return ranked_vaults[0][0]

    best_vault, best_vault_score = ranked_vaults[0]
    second_vault, second_vault_score = ranked_vaults[1]
    if best_vault_score >= _MIN_CONFIDENCE and (best_vault_score - second_vault_score) >= 0.15:
        logger.info(
            "[vault-router] unambiguous suggestion=%s score=%.3f margin=%.3f",
            best_vault,
            best_vault_score,
            best_vault_score - second_vault_score,
        )
        return best_vault
    logger.debug(
        "[vault-router] ambiguous or low-confidence matches=%s best=%.3f second=%.3f",
        [vault for vault, _ in ranked_vaults],
        best_vault_score,
        second_vault_score,
    )
    return None
