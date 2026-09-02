"""Tests for the experimental semantic vault-routing overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vault_router


def _write_index(tmp_path: Path, entries):
    index = tmp_path / "vault-router-index.local.json"
    index.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return index


def test_suggest_vault_returns_none_when_index_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("Inspector Pentair") is None


def test_suggest_vault_ignores_empty_entries(monkeypatch, tmp_path):
    _write_index(tmp_path, [{"vault_id": "", "title": "", "concepts": []}])
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("anything") is None


def test_suggest_vault_matches_unique_concept(monkeypatch, tmp_path):
    _write_index(
        tmp_path,
        [
            {"vault_id": "core", "title": "Core", "concepts": ["BDH routing"]},
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["Inspector", "Pentair"]},
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("Come funziona l'Inspector su Pentair?") == "crossnection"


def test_suggest_vault_matches_vault_name(monkeypatch, tmp_path):
    _write_index(
        tmp_path,
        [
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["Inspector"]},
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("Crossnection -> Inspector | Pentair") == "crossnection"


def test_suggest_vault_returns_none_when_ambiguous(monkeypatch, tmp_path):
    _write_index(
        tmp_path,
        [
            {"vault_id": "core", "title": "Core", "concepts": ["automation", "routing"]},
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["automation", "Inspector"]},
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("automation") is None


def test_suggest_vault_returns_none_below_threshold(monkeypatch, tmp_path):
    _write_index(
        tmp_path,
        [
            {"vault_id": "core", "title": "Core", "concepts": ["BDH"]},
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("ciao") is None


def test_suggest_vault_returns_none_when_single_vault_below_threshold(monkeypatch, tmp_path):
    _write_index(
        tmp_path,
        [
            {"vault_id": "core", "title": "Core", "concepts": ["BDH"]},
            {"vault_id": "core", "title": "Core routing", "concepts": ["BDH"]},
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert vault_router.suggest_vault("ciao") is None
