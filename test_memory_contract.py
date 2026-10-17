"""Tests for Phase 0 of the two-tier memory plan (docs/memory-policy-plan.md).

Covers exactly the plan's "Minimum required tests": positive classification,
negative classification, conflict resolution, and provenance. Importing or
exercising this module must never reach a live Honcho/BDH write — it is pure
classification with no I/O.
"""

from __future__ import annotations

import memory_contract as mc


# --- Positive -----------------------------------------------------------

def test_decision_pattern_routes_to_bdh_as_project_decision():
    candidate = mc.classify(
        "Abbiamo deciso di usare oMLX al posto di mlx-lm per l'inferenza locale.",
        scope={"type": "project", "id": "bdh-hermes-bridge"},
        source={"session": "sess-1", "message": "msg-10"},
    )
    assert candidate.kind is mc.MemoryKind.PROJECT_DECISION
    assert candidate.owner == "bdh"
    assert candidate.status == "routed"


def test_fact_pattern_routes_to_bdh_as_project_fact():
    candidate = mc.classify(
        "Il watcher ricostruisce l'indice BM25 ad ogni refresh del grafo.",
        scope={"type": "project", "id": "bdh-graph-harness"},
        source={"session": "sess-2", "message": "msg-4"},
    )
    assert candidate.kind is mc.MemoryKind.PROJECT_FACT
    assert candidate.owner == "bdh"
    assert candidate.status == "routed"


# --- Negative -------------------------------------------------------------

def test_preference_routes_to_honcho_not_bdh():
    candidate = mc.classify(
        "Preferisco risposte concise, senza politeness theater.",
        source={"session": "sess-3"},
    )
    assert candidate.kind is mc.MemoryKind.USER_PROFILE
    assert candidate.owner == "honcho"


def test_smalltalk_is_discarded():
    candidate = mc.classify("ciao grazie")
    assert candidate.kind is mc.MemoryKind.DISCARD
    assert candidate.owner is None
    assert candidate.status == "discarded"


def test_frustration_venting_never_reaches_bdh_or_honcho():
    candidate = mc.classify(
        "Mi ha fatto perdere tempo questo bug.",
        suggested_kind="project_lesson",  # even if upstream suggests project_lesson
        scope={"type": "project", "id": "hermes-mission-control"},
    )
    assert candidate.owner not in ("bdh", "honcho")
    assert candidate.kind is mc.MemoryKind.EPISODIC_ONLY


def test_unrecognised_suggested_kind_fails_closed_to_episodic():
    candidate = mc.classify("qualcosa di ambiguo", suggested_kind="totally_unknown_kind")
    assert candidate.kind is mc.MemoryKind.EPISODIC_ONLY
    assert candidate.owner == "episodic"
    assert candidate.reason == "unrecognised_kind"


def test_project_kind_without_scope_fails_closed_instead_of_guessing_a_vault():
    candidate = mc.classify(
        "Nuova architettura di routing proposta.",
        suggested_kind="project_concept",
    )
    assert candidate.kind is mc.MemoryKind.EPISODIC_ONLY
    assert candidate.reason == "project_kind_missing_scope"


def test_policy_constraint_requires_human_review_not_direct_write():
    candidate = mc.classify(
        "D'ora in poi niente push diretti su main senza approvazione.",
        suggested_kind="policy_constraint",
    )
    assert candidate.kind is mc.MemoryKind.POLICY_CONSTRAINT
    assert candidate.status == "needs_review"
    assert candidate.owner == "policy"


# --- Conflict ---------------------------------------------------------------

def test_scoped_bdh_decision_beats_global_honcho_preference():
    honcho_preference = mc.classify(
        "Preferisco risposte dirette, senza fronzoli.",
        source={"session": "sess-4"},
    )
    bdh_decision = mc.classify(
        "Abbiamo deciso che il merge su Curate fallisce chiuso in caso di conflitto.",
        scope={"type": "project", "id": "bdh-graph-harness"},
        source={"session": "sess-4", "message": "msg-9"},
    )
    winner = mc.resolve_precedence([honcho_preference, bdh_decision])
    assert winner is bdh_decision
    assert winner is not None
    assert winner.owner == "bdh"


def test_resolve_precedence_ignores_discarded_and_needs_review_candidates():
    discarded = mc.classify("ciao")
    needs_review = mc.classify("vincolo di policy", suggested_kind="policy_constraint")
    only_routable = mc.classify(
        "Preferisco il dark mode.",
        source={"session": "sess-5"},
    )
    winner = mc.resolve_precedence([discarded, needs_review, only_routable])
    assert winner is only_routable


def test_resolve_precedence_returns_none_when_nothing_is_routable():
    discarded = mc.classify("ciao")
    assert mc.resolve_precedence([discarded]) is None


# --- Provenance --------------------------------------------------------------

def test_every_routed_candidate_answers_session_scope_confidence_status():
    candidate = mc.classify(
        "Abbiamo deciso di ruotare il token MC.",
        scope={"type": "project", "id": "hermes-mission-control"},
        source={"session": "sess-6", "platform": "tui", "message": "msg-1", "timestamp": 1234.0},
    )
    assert candidate.source.session == "sess-6"
    assert candidate.scope.id == "hermes-mission-control"
    assert candidate.confidence in ("high", "medium", "low")
    assert candidate.status in ("routed", "needs_review", "discarded")


def test_missing_provenance_still_classifies_but_scope_and_source_default_safely():
    candidate = mc.classify("Preferisco il tema scuro.")
    assert candidate.source.session is None
    assert candidate.scope.id is None
    assert candidate.owner == "honcho"  # provenance gaps don't block Honcho (no vault to pick)


def test_malformed_scope_and_source_inputs_do_not_raise():
    candidate = mc.classify("ciao", scope="not-a-mapping", source=123)  # type: ignore[arg-type]
    assert candidate.kind is mc.MemoryKind.DISCARD
