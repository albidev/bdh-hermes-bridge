"""The minimum-turn floor has ONE owner, and both synthesis paths must agree.

Why this is load-bearing
------------------------
Two independent paths synthesise a session:

``session_synthesis_watcher.py``   standalone daemon, serves TUI / Mission Control
``__init__.py`` (bridge)           in-process idle watcher, serves the rest

The floor used to be duplicated: the bridge read
``BDH_SESSION_SYNTH_MIN_TURNS`` while the standalone watcher compared against a
literal ``3``. Configuring the variable therefore changed ONE path and silently
left the other where it was — the same "two resolvers, one decision" drift that a
split home resolver produced, and invisible from the outside because both paths
produce no output when they skip.

It mattered concretely: the floor of 3 was justified as "a 1-turn session is
already covered by the per-turn write path", but that path is gated behind
``BDH_QUERY_REWRITE_ENABLED`` (opt-in, unset), so nothing is written per turn.
The floor was dropping exactly the sessions it claimed were covered elsewhere.

The tests below reload the bridge with the variable REMOVED, because the
operator's ``~/.hermes/.env`` exports it: asserting the default against an
inherited value would test the environment rather than the code.
"""

from __future__ import annotations

import importlib

import pytest

import session_synthesis_watcher as watcher_mod

# The variable is exported by the operator's ~/.hermes/.env, and the bridge reads
# it at IMPORT time, so a default-value assertion must control it explicitly.
_ENV_VAR = "BDH_SESSION_SYNTH_MIN_TURNS"


def _fresh_bridge_without_env(monkeypatch):
    """Import the bridge with the floor variable absent, returning the module."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    return importlib.reload(importlib.import_module("__init__"))


def _bridge():
    return importlib.import_module("__init__")


def test_both_paths_read_the_same_floor():
    """The standalone watcher must not carry its own literal."""
    bridge = _bridge()
    assert watcher_mod._min_turns() == bridge._SESSION_SYNTH_MIN_TURNS


def test_default_floor_is_one_when_unset(monkeypatch):
    """A single well-answered exchange is worth synthesising.

    With the actor gate deciding WHICH sessions qualify, the floor only needs to
    exclude a session with nothing to learn from. One completed exchange carries
    a concept; requiring three dropped short, high-signal exchanges.

    Reloaded with the variable deleted: the operator's env exports 3, so without
    this the assertion would read the environment, not the default.
    """
    bridge = _fresh_bridge_without_env(monkeypatch)
    assert bridge._SESSION_SYNTH_MIN_TURNS == 1, (
        "the built-in default floor must be 1: the per-turn write path that "
        "justified a higher floor is opt-in (BDH_QUERY_REWRITE_ENABLED) and unset"
    )


def test_a_configured_floor_reaches_both_paths(monkeypatch):
    """The variable must change BOTH paths, not just the bridge."""
    monkeypatch.setenv(_ENV_VAR, "5")
    bridge = importlib.reload(_bridge())
    assert bridge._SESSION_SYNTH_MIN_TURNS == 5
    assert watcher_mod._min_turns() == 5, (
        "the standalone watcher must follow the same floor, or a configured "
        "value changes one path and silently leaves the other"
    )


def test_watcher_floor_is_never_below_one(monkeypatch):
    """Zero would make every session eligible, including empty ones."""
    monkeypatch.setenv(_ENV_VAR, "0")
    bridge = importlib.reload(_bridge())
    assert bridge._SESSION_SYNTH_MIN_TURNS == 1
    assert watcher_mod._min_turns() == 1


def test_floor_survives_an_unimportable_bridge(monkeypatch):
    """A loader without the package must still honour the configured floor.

    The standalone watcher is run by launchd as a plain module, so the bridge is
    reachable only when the repo is on sys.path. Losing it must degrade the
    floor's source, never the watcher.
    """
    monkeypatch.setenv(_ENV_VAR, "4")
    real_import = importlib.import_module

    def explode(name, *args, **kwargs):
        if name.endswith("__init__"):
            raise ImportError("simulated: bridge not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", explode)
    assert watcher_mod._min_turns() == 4


def test_garbage_floor_falls_back_to_one(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "not-a-number")
    real_import = importlib.import_module

    def explode(name, *args, **kwargs):
        if name.endswith("__init__"):
            raise ImportError("simulated: bridge not importable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", explode)
    assert watcher_mod._min_turns() == 1
