# Operational Plan — Two-Tier Hermes Memory (Honcho + BDH)

> Canonical plan issue: https://github.com/albidev/bdh-hermes-bridge/issues/5
> Vault mirror: `~/Documents/Hermes/wiki/plans/two-tier-memory-honcho-bdh.md`

**Goal:** Separate the *user model* (Honcho) from *project knowledge* (BDH) with an
ownership / scope / provenance contract, without touching the Hermes core.

**Architecture:**

```
Hermes MemoryManager
   └── Honcho            = user profile / preferences / patterns
BDH bridge plugin
   ├── read  (pre_llm_call, already implemented)
   └── write (post_api_request, to be constrained)
Policy/contract layer
   └── decides ownership + scope + provenance
```

**Hard constraint:** `MemoryManager` allows only one external provider → BDH must
**not** be registered as a second `MemoryProvider`. Changes happen in the bridge.

---

## Phase 0 — Memory contract (no behavior change)

New module `memory_contract.py` in `bdh-hermes-bridge`.

- Type enum: `user_profile`, `project_fact`, `project_decision`,
  `project_concept`, `project_lesson`, `policy_constraint`, `episodic_only`,
  `discard`.
- Candidate structure: `kind`, `content`, `scope{type,id}`, `owner`,
  `confidence`, `source{session,platform,message,timestamp}`, `status`.
- Rule: `owner` = where it may become persistent memory; `scope` = how far it can
  be reused.
- Tests: positive / negative / conflict classification.

## Phase 1 — `observe-only` mode

- Bridge config: `memory_policy.mode: observe`, log candidates, no new writes.
- Measure a few days of real usage:
  - decisions recognized vs missed;
  - preferences wrongly sent to BDH;
  - noise promoted;
  - `unknown` cases.
- **Go/No-go:** only after real data move to enforce.

## Phase 2 — Enforce write path

- `post_api_request` sends to BDH **only** `project_*` (fact / decision /
  concept / lesson).
- Honcho still receives the turn for its own user model.
- `policy_constraint` → config / policy, not semantic memory.
- `episodic_only` → session history. `discard` → ignored.
- Fix: a length threshold is not enough; semantic classification is required
  before any BDH write.

## Phase 3 — Explicit read path

- Two separate prompt blocks: `USER CONTEXT` (Honcho) and
  `PROJECT KNOWLEDGE` (BDH). No single `memory-context` block.
- Precedence: policy > scoped decision > fact with provenance > global
  preference > weak inference.
- A scoped decision beats a global user preference.

## Phase 4 — Provenance on BDH

- `bdh-graph-harness`: the write path accepts `scope`, `kind`, `source`,
  `status`.
- Notes carry an explicit type: `decision` / `fact` / `lesson` / `concept`.
- `superseded` status for obsolete decisions (tombstone, not blind delete).

---

## Files to touch

- `bdh-hermes-bridge/memory_contract.py` (new)
- `bdh-hermes-bridge/__init__.py` (apply policy, observe/enforce modes,
  routing log)
- `bdh-hermes-bridge/test_memory_contract.py` (new)
- `bdh-hermes-bridge/test_bdh_bridge.py` (extend with routing + conflict)
- `bdh-graph-harness`: notes with metadata; no new service / DB.

**Hermes core:** no changes in Phases 0–4.

---

## Minimum required tests

- **Positive:** "we decided X" → `project_decision` in BDH; "the watcher
  rebuilds BM25" → `project_fact`.
- **Negative:** "I prefer concise answers" → Honcho; "hi thanks" →
  `discard`; "that wasted my time" → never BDH.
- **Conflict:** global Honcho preference vs scoped BDH decision → BDH wins.
- **Provenance:** every promoted memory must answer session / message /
  scope / confidence / status.

## Success criteria

- Honcho without regressions.
- BDH no longer receives generic preferences.
- Technical decisions land in the vault with provenance.
- Casual queries do not trigger writes.
- One project does not contaminate another vault.
- The system can explain why a candidate was written or discarded.

## Execution order

1. Memory contract
2. Observe-only
3. Tests on real conversations
4. Enforce BDH
5. BDH note metadata
6. Conflict resolution
7. (only after) possible Hermes core extension

First concrete change: an **observable ownership router** in front of BDH writes,
with Honcho / Hermes intact until we have data on the ambiguous cases.
