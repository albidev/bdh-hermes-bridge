"""Tests for the experimental semantic vault-routing overlay.

The overlay resolves its index from ``BDH_VAULT_ROUTER_INDEX``, falling back to
``vault-router-index.local.json`` in the CURRENT WORKING DIRECTORY. Both inputs
are ambient, and this suite must not inherit either:

- the operator's real index path is exported (``~/.hermes/.env``, and the
  gateway plist carries it), so a test that means "index missing" or "index has
  these two entries" would silently score against the 296 real entries instead;
- the CWD is whatever the runner happens to be in.

``test_suggest_vault_returns_none_when_index_missing`` was the visible symptom: it
chdir'd to an empty tmp_path and asserted ``None``, but the exported env var won,
the real index loaded, and ``"Inspector Pentair"`` legitimately matched
``crossnection``. The test failed on a developer machine with a configured index
and passed in CI (where the variable is unset) — the worst distribution of a red
signal: it teaches you to distrust the suite.

The autouse fixture below pins BOTH inputs for every test in this file, so each
case asserts against the index it actually wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vault_router


@pytest.fixture(autouse=True)
def _isolate_index(monkeypatch, tmp_path):
    """Pin the index source and the CWD for every test in this module.

    Deleting the env var is the load-bearing part: an exported
    ``BDH_VAULT_ROUTER_INDEX`` outranks the CWD, so without this the tests below
    are decided by the operator's environment rather than by their fixtures.
    """
    monkeypatch.delenv("BDH_VAULT_ROUTER_INDEX", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_index(tmp_path: Path, entries):
    index = tmp_path / "vault-router-index.local.json"
    index.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return index


def test_suggest_vault_returns_none_when_index_missing():
    assert vault_router.suggest_vault("Inspector Pentair") is None


def test_an_exported_index_path_would_outrank_the_cwd(monkeypatch, tmp_path):
    """The isolation above is load-bearing, not decorative.

    Reproduces the original defect: with the env var pointing at an index that
    matches, the CWD-local (absent) file is irrelevant and a suggestion is
    returned. Pinning this documents WHY the fixture deletes the variable.

    The query matches two concepts, which is what clears ``_MIN_CONFIDENCE``
    (0.2 + 2 * 0.15 = 0.5): a single bare concept scores 0.35 and returns None
    for reasons unrelated to this test's point.
    """
    real_index = _write_index(
        tmp_path,
        [{
            "vault_id": "crossnection",
            "title": "Crossnection",
            "concepts": ["Inspector", "Pentair"],
        }],
    )
    empty_cwd = tmp_path / "empty"
    empty_cwd.mkdir()
    monkeypatch.chdir(empty_cwd)
    monkeypatch.setenv("BDH_VAULT_ROUTER_INDEX", str(real_index))

    assert vault_router.suggest_vault("Inspector Pentair") == "crossnection", (
        "an exported index path must outrank the CWD — this is the behaviour that "
        "made the 'index missing' test fail on a configured machine"
    )


def test_suggest_vault_ignores_empty_entries():
    _write_index(tmp_path=_cwd(), entries=[{"vault_id": "", "title": "", "concepts": []}])
    assert vault_router.suggest_vault("anything") is None


def test_suggest_vault_matches_unique_concept():
    _write_index(
        _cwd(),
        [
            {"vault_id": "core", "title": "Core", "concepts": ["BDH routing"]},
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["Inspector", "Pentair"]},
        ],
    )
    assert vault_router.suggest_vault("Come funziona l'Inspector su Pentair?") == "crossnection"


def test_suggest_vault_matches_vault_name():
    _write_index(
        _cwd(),
        [
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["Inspector"]},
        ],
    )
    assert vault_router.suggest_vault("Crossnection -> Inspector | Pentair") == "crossnection"


def test_suggest_vault_returns_none_when_ambiguous():
    _write_index(
        _cwd(),
        [
            {"vault_id": "core", "title": "Core", "concepts": ["automation", "routing"]},
            {"vault_id": "crossnection", "title": "Crossnection", "concepts": ["automation", "Inspector"]},
        ],
    )
    assert vault_router.suggest_vault("automation") is None


def test_suggest_vault_returns_none_below_threshold():
    _write_index(_cwd(), [{"vault_id": "core", "title": "Core", "concepts": ["BDH"]}])
    assert vault_router.suggest_vault("ciao") is None


def test_suggest_vault_returns_none_when_single_vault_below_threshold():
    _write_index(
        _cwd(),
        [
            {"vault_id": "core", "title": "Core", "concepts": ["BDH"]},
            {"vault_id": "core", "title": "Core routing", "concepts": ["BDH"]},
        ],
    )
    assert vault_router.suggest_vault("ciao") is None


def test_missing_index_path_does_not_raise(monkeypatch, tmp_path):
    """An explicit path that does not exist disables the overlay, not the caller.

    The overlay is additive: the module docstring promises it returns None when
    it cannot suggest, so a bad path must never propagate an exception into the
    query path.
    """
    monkeypatch.setenv("BDH_VAULT_ROUTER_INDEX", str(tmp_path / "nope.json"))
    assert vault_router.suggest_vault("Inspector") is None


def test_invalid_index_file_disables_the_overlay(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not a list", encoding="utf-8")
    monkeypatch.setenv("BDH_VAULT_ROUTER_INDEX", str(bad))
    assert vault_router.suggest_vault("Inspector") is None


def _cwd() -> Path:
    """The isolated CWD the autouse fixture pinned, as a Path."""
    return Path.cwd()
