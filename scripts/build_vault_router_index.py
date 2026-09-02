"""Bootstrap script for the BDH semantic vault-router index.

Scans vault note metadata and writes a local routing index
`vault-router-index.local.json` with schema:
  {vault_id, title, concepts}

This script is intentionally agnostic: no hardcoded vault names,
no private paths, no hardcoded vault_ids. Configure via CLI args.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_OUTPUT = "vault-router-index.local.json"
DEFAULT_MIN_NODE_SIZE = 1024
GENERIC_TITLES = {
    "readme",
    "changelog",
    "best practices",
    "contributing",
    "license",
    "todo",
    "index",
    "log",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local vault-router index from vault metadata."
    )
    parser.add_argument(
        "--vault",
        action="append",
        required=True,
        help="Vault root path. Repeatable. Each vault must contain markdown notes.",
    )
    parser.add_argument(
        "--vault-id",
        action="append",
        help="Explicit vault identifier for the corresponding --vault path. "
             "If omitted, the basename of the vault path is used.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output index file path. Default: %(default)s",
    )
    parser.add_argument(
        "--min-node-size",
        type=int,
        default=DEFAULT_MIN_NODE_SIZE,
        help="Minimum file size in bytes to include a note. Default: %(default)s",
    )
    return parser.parse_args()


def _iter_markdown_files(vault_path: Path) -> Iterable[Path]:
    for path in vault_path.rglob("*.md"):
        if path.is_file():
            yield path


def _extract_title(path: Path) -> str:
    stem = path.stem
    title = stem.replace("-", " ").replace("_", " ").strip()
    return title


def _extract_concepts(path: Path) -> list[str]:
    concepts: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return concepts

    for line in text.splitlines()[:40]:
        if line.strip().startswith("#"):
            heading = line.strip().lstrip("#").strip()
            if heading:
                concepts.append(heading)
    return concepts


def _is_generic_title(title: str) -> bool:
    normalized = title.strip().lower()
    return normalized in GENERIC_TITLES or normalized.startswith("heading ") or normalized.startswith("section ")


def _build_vault_entries(
    vault_path: Path,
    vault_id: str,
    min_node_size: int,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for md in _iter_markdown_files(vault_path):
        try:
            size = md.stat().st_size
        except OSError:
            continue
        if size < min_node_size:
            continue

        title = _extract_title(md)
        if not title or _is_generic_title(title):
            continue

        concepts = _extract_concepts(md)
        entries.append({
            "vault_id": vault_id,
            "title": title,
            "concepts": concepts,
        })
    return entries


def _deduplicate(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, object]] = []
    for entry in entries:
        key = (str(entry["vault_id"]), str(entry["title"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def main() -> int:
    args = _parse_args()
    vault_paths = [Path(p).expanduser().resolve() for p in args.vault]
    vault_ids = args.vault_id or [p.name for p in vault_paths]

    if len(vault_paths) != len(vault_ids):
        raise SystemExit("Error: --vault-id count must match --vault count, or omit --vault-id entirely.")

    entries: list[dict[str, object]] = []
    for vault_path, vault_id in zip(vault_paths, vault_ids):
        if not vault_path.exists():
            raise SystemExit(f"Error: vault path does not exist: {vault_path}")
        entries.extend(_build_vault_entries(vault_path, vault_id, args.min_node_size))

    entries = _deduplicate(entries)

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
