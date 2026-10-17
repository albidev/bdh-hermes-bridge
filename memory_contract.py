"""Phase 0 of the two-tier memory plan (Honcho vs BDH ownership).

See docs/memory-policy-plan.md. This module performs CLASSIFICATION ONLY: it
decides which system (Honcho, BDH, policy/config, or nothing) a piece of
conversational content should eventually belong to, and records why. It is
not wired into ``__init__.py`` yet — importing this module changes no runtime
behavior (Phase 0 constraint). Phase 1 wires it in ``observe`` mode (log
only); Phase 2 constrains the real ``post_api_request`` write path.

The design mirrors the proven pattern used by the (retired) nightly
consolidation pipeline's ``EvidenceRouter``: an LLM or upstream heuristic may
*suggest* a kind, but this module *owns* the final ownership decision, and
unknown/ambiguous/structurally incomplete input fails closed — it is never
silently promoted to BDH or Honcho as durable memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class MemoryKind(str, Enum):
    USER_PROFILE = "user_profile"
    PROJECT_FACT = "project_fact"
    PROJECT_DECISION = "project_decision"
    PROJECT_CONCEPT = "project_concept"
    PROJECT_LESSON = "project_lesson"
    POLICY_CONSTRAINT = "policy_constraint"
    EPISODIC_ONLY = "episodic_only"
    DISCARD = "discard"


PROJECT_KINDS = frozenset(
    {
        MemoryKind.PROJECT_FACT,
        MemoryKind.PROJECT_DECISION,
        MemoryKind.PROJECT_CONCEPT,
        MemoryKind.PROJECT_LESSON,
    }
)

_KIND_VALUES = {kind.value: kind for kind in MemoryKind}


@dataclass(frozen=True)
class Scope:
    """How far a candidate can be reused. ``type`` is e.g. 'project' or
    'global'; ``id`` is the concrete project/vault identifier when scoped."""

    type: str = "unscoped"
    id: Optional[str] = None


@dataclass(frozen=True)
class Source:
    """Provenance: every promoted candidate must answer session / message."""

    session: Optional[str] = None
    platform: Optional[str] = None
    message: Optional[str] = None
    timestamp: Optional[float] = None


@dataclass(frozen=True)
class Candidate:
    kind: MemoryKind
    content: str
    scope: Scope
    owner: Optional[str]  # "honcho" | "bdh" | "policy" | "episodic" | None
    confidence: str  # "high" | "medium" | "low"
    source: Source
    status: str  # "routed" | "needs_review" | "discarded"
    reason: str


# --- Deterministic signal patterns -----------------------------------------
# These only ever STRENGTHEN or OVERRIDE a weak/missing upstream suggestion;
# they never silently downgrade an explicit, well-formed one. Conservative on
# purpose: a miss falls through to the LLM-suggested kind, not the reverse.

_DECISION_PATTERN = re.compile(
    r"\b(abbiamo deciso|si è deciso|decidiamo di|we('| ha)ve decided|we decided)\b",
    re.IGNORECASE,
)
_FACT_PATTERN = re.compile(
    r"\b(il watcher|lo script .*(ricostruisce|rigenera)|the watcher|the script .*rebuilds?)\b",
    re.IGNORECASE,
)
_PREFERENCE_PATTERN = re.compile(
    r"\b(preferisco|mi piace|i prefer|i('| a)m a fan of|non mi piace|i don'?t like)\b",
    re.IGNORECASE,
)
_SMALLTALK_PATTERN = re.compile(
    r"^\s*(ciao([,!]? grazie)?|grazie( mille)?|ok(ay)?|hi|hello|hey|thanks( a lot)?|thank you)\s*[!.]*\s*$",
    re.IGNORECASE,
)
_FRUSTRATION_PATTERN = re.compile(
    r"\b(mi ha fatto perdere tempo|waste(d)? my time|che perdita di tempo|mi fa perdere tempo)\b",
    re.IGNORECASE,
)


def _safe_scope(scope: Optional[Mapping[str, Any]]) -> Scope:
    if not isinstance(scope, Mapping):
        return Scope()
    scope_type = str(scope.get("type") or "unscoped")
    scope_id = scope.get("id")
    return Scope(type=scope_type, id=str(scope_id) if scope_id else None)


def _safe_source(source: Optional[Mapping[str, Any]]) -> Source:
    if not isinstance(source, Mapping):
        return Source()
    return Source(
        session=_opt_str(source.get("session")),
        platform=_opt_str(source.get("platform")),
        message=_opt_str(source.get("message")),
        timestamp=source.get("timestamp") if isinstance(source.get("timestamp"), (int, float)) else None,
    )


def _opt_str(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def _parse_kind(raw: Optional[str]) -> Optional[MemoryKind]:
    if not raw:
        return None
    return _KIND_VALUES.get(str(raw).strip().lower())


def _candidate(
    kind: MemoryKind,
    content: str,
    scope: Scope,
    source: Source,
    *,
    owner: Optional[str],
    confidence: str,
    status: str,
    reason: str,
) -> Candidate:
    return Candidate(
        kind=kind,
        content=content,
        scope=scope,
        owner=owner,
        confidence=confidence,
        source=source,
        status=status,
        reason=reason,
    )


def _discard(content: str, scope: Scope, source: Source, reason: str) -> Candidate:
    return _candidate(
        MemoryKind.DISCARD, content, scope, source,
        owner=None, confidence="high", status="discarded", reason=reason,
    )


def _episodic_only(content: str, scope: Scope, source: Source, reason: str) -> Candidate:
    """Routed to session history only — never Honcho, never BDH.

    This is the fail-closed landing zone: unrecognised kinds, missing scope
    on a project-shaped candidate, and venting/frustration all land here
    instead of silently becoming durable memory somewhere.
    """
    return _candidate(
        MemoryKind.EPISODIC_ONLY, content, scope, source,
        owner="episodic", confidence="low", status="routed", reason=reason,
    )


def _user_profile(content: str, scope: Scope, source: Source, reason: str, confidence: str) -> Candidate:
    return _candidate(
        MemoryKind.USER_PROFILE, content, scope, source,
        owner="honcho", confidence=confidence, status="routed", reason=reason,
    )


def _project(kind: MemoryKind, content: str, scope: Scope, source: Source, reason: str, confidence: str) -> Candidate:
    return _candidate(
        kind, content, scope, source,
        owner="bdh", confidence=confidence, status="routed", reason=reason,
    )


def classify(
    content: str,
    *,
    suggested_kind: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    source: Optional[Mapping[str, Any]] = None,
    llm_confidence: Optional[str] = None,
) -> Candidate:
    """Classify a conversational snippet into a :class:`Candidate`.

    ``suggested_kind`` is whatever an upstream LLM/heuristic proposed (may be
    ``None`` or an invalid string); this function owns the final decision.
    Deterministic patterns are checked first because they are cheap and
    unambiguous; an LLM suggestion is only consulted when no pattern fires.
    """
    text = (content or "").strip()
    scp = _safe_scope(scope)
    src = _safe_source(source)

    if not text:
        return _discard(text, scp, src, "empty_content")

    if _SMALLTALK_PATTERN.match(text):
        return _discard(text, scp, src, "smalltalk")

    if _FRUSTRATION_PATTERN.search(text):
        return _episodic_only(text, scp, src, "frustration_venting")

    if _DECISION_PATTERN.search(text):
        return _project(MemoryKind.PROJECT_DECISION, text, scp, src, "decision_pattern", "high")

    if _FACT_PATTERN.search(text):
        return _project(MemoryKind.PROJECT_FACT, text, scp, src, "fact_pattern", "medium")

    if _PREFERENCE_PATTERN.search(text):
        return _user_profile(text, scp, src, "preference_pattern", "high")

    kind = _parse_kind(suggested_kind)
    if kind is None:
        return _episodic_only(text, scp, src, "unrecognised_kind")

    if kind is MemoryKind.DISCARD:
        return _discard(text, scp, src, "llm_suggested_discard")

    if kind is MemoryKind.EPISODIC_ONLY:
        return _episodic_only(text, scp, src, "llm_suggested_episodic")

    if kind is MemoryKind.USER_PROFILE:
        return _user_profile(text, scp, src, "llm_suggested_user_profile", llm_confidence or "medium")

    if kind is MemoryKind.POLICY_CONSTRAINT:
        return _candidate(
            kind, text, scp, src,
            owner="policy", confidence=llm_confidence or "medium",
            status="needs_review", reason="policy_constraint_requires_human_review",
        )

    if kind in PROJECT_KINDS:
        if not scp.id:
            # A project-shaped candidate with no concrete scope cannot be
            # routed to a BDH vault safely — fail closed rather than guess.
            return _episodic_only(text, scp, src, "project_kind_missing_scope")
        return _project(kind, text, scp, src, "llm_suggested_project_kind", llm_confidence or "medium")

    return _episodic_only(text, scp, src, "unhandled_kind")  # pragma: no cover - exhaustive enum guard


# --- Phase 3 precedence (kept here so the plan's conflict test has a home) --

_PRECEDENCE_RANK: Mapping[MemoryKind, int] = {
    MemoryKind.POLICY_CONSTRAINT: 0,
    MemoryKind.PROJECT_DECISION: 1,
    MemoryKind.PROJECT_FACT: 1,
    MemoryKind.PROJECT_CONCEPT: 1,
    MemoryKind.PROJECT_LESSON: 1,
    MemoryKind.USER_PROFILE: 2,
    MemoryKind.EPISODIC_ONLY: 3,
    MemoryKind.DISCARD: 4,
}


def resolve_precedence(candidates: Sequence[Candidate]) -> Optional[Candidate]:
    """Pick the winning candidate when several conflict on the same topic.

    Precedence (plan Phase 3): policy > scoped project fact/decision with
    provenance > global user preference > weak inference. A scoped BDH
    decision beats a general Honcho preference — this is the behaviour the
    plan's required conflict test pins down. Only ``routed`` candidates with
    an owner participate; discarded/needs_review candidates never win.
    """
    routable = [c for c in candidates if c.status == "routed" and c.owner]
    if not routable:
        return None

    def rank(candidate: Candidate) -> tuple:
        base = _PRECEDENCE_RANK.get(candidate.kind, 5)
        has_provenance = 0 if (candidate.source.session and candidate.scope.id) else 1
        return (base, has_provenance)

    return sorted(routable, key=rank)[0]
