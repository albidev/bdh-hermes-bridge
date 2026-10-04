# Session-end synthesis

Session-end synthesis is the bridge's **session-level write path**. It turns a
multi-turn conversation into one bounded, curated learning request after the
session ends. The goal is to preserve the durable insight that emerged across
the conversation, rather than storing only isolated per-turn fragments.

It is intentionally separate from automatic retrieval:

- `pre_llm_call` reads context from BDH with `learn=false` and never changes
  memory;
- `post_api_request` writes an eligible user/assistant turn to BDH;
- session-end synthesis writes one additional, session-level request when the
  lifecycle boundary is reached.

## Why it exists

A debugging or design conversation often reaches its useful conclusion only
after several turns. The per-turn write path may capture the observations, but
not the relationship between them or the final decision.

For example:

```text
Turn 1: observe a routing failure
Turn 2: identify a forced default vault
Turn 3: remove the override and use the local router
Turn 4: verify the resolved vault
```

The synthesis should preserve the reusable lesson:

> The forced default bypassed semantic vault routing; removing it restores
> query-specific vault selection.

It should not create a note for every transient status message.

## Lifecycle

```text
pre_llm_call
  ├─ resolve deterministic scope
  ├─ apply the local semantic vault-router overlay if needed
  ├─ retrieve optional BDH context (read-only)
  └─ capture turn state
        │
        ▼
post_api_request
  ├─ apply the write/routing gate
  ├─ if store_candidate=true:
  │     submit the normal per-turn write asynchronously
  │     after a successful write, buffer as accepted
  └─ if store_candidate=false and the turn is safe:
        buffer as context-only (no direct write)
        │
        ▼
finalize / reset / session boundary
  ├─ wait logically for in-flight per-turn writes (without blocking the hook)
  ├─ discard short, failed, or mixed-scope buffers
  ├─ bound the transcript
  └─ submit one `source=session_synthesis` request to BDH

idle (bridge-owned watcher calls _on_session_idle)
  ├─ wait logically for in-flight per-turn writes (without blocking the hook)
  ├─ if the current epoch has fewer than min-turns, leave it open (non-destructive)
  ├─ otherwise drain the epoch, bound the transcript, and submit one
  │   `source=session_synthesis` request — WITHOUT resetting the session
  └─ a later turn starts a new epoch, eligible for its own idle/finalize flush
```

The implementation registers Hermes `on_session_finalize` and
`on_session_reset`; the bridge-owned idle watcher calls `_on_session_idle` directly.
There is no registered Hermes `on_session_idle` hook. Session identity rotation
remains supported as a compatibility boundary, but a new turn is not required
for a finalized session to flush.

## Idle flush and epochs

An idle flush is **non-destructive**. It stages at most one candidate synthesis
for the current buffered epoch and leaves the Hermes session alive. The scope
binding is retained, so the session keeps routing to the same vault when it
becomes active again.

- **At-most-once per epoch:** once an epoch is drained by an idle flush, a
  repeated idle notification finds an empty buffer and does nothing.
- **New epoch on activity:** the first turn buffered after an idle flush starts
  a fresh epoch, eligible for a later idle or finalize flush.
- **Below min-turns:** an idle flush with too few turns leaves the buffer
  intact, so the epoch stays open and can still be synthesized once enough
  turns accumulate.
- **Finalize/reset remain authoritative:** they drain whatever remains and
  permanently close the session, so a late callback can never resurrect it.
  Finalize after an idle flush does not re-flush the already-drained epoch.

## What is buffered

A turn enters the synthesis buffer only when it is safe for learning. There are
now two categories:

- **Accepted** — the turn was written directly to BDH (`store_candidate=true`)
  and the HTTP write succeeded. These turns are buffered from the `on_success`
  callback.
- **Context-only** — the turn was not durable enough for a direct write
  (`store_candidate=false`) but the response is complete and safe. These turns
  carry causal context needed by later turns in the same session; they are
  buffered immediately without calling BDH.

Each buffered item contains:

- the user message, capped at 4,000 characters;
- the assistant response, capped at 12,000 characters;
- the resolved `vault_id` for that turn;
- a `context_only` boolean flag distinguishing the two categories.

The in-memory buffer is capped at 200 turns per session. The final transcript
is capped by `BDH_SESSION_SYNTH_MAX_CHARS` and is rendered as `USER:` /
`ASSISTANT:` pairs.

The following are excluded from the buffer:

- failed per-turn writes, including timeouts (`BDH_PER_TURN_TIMEOUT`) and writes
  dropped because every `BDH_PER_TURN_MAX_INFLIGHT` slot was busy;
- truncated (`finish_reason != stop`) responses;
- blacklisted prompts;
- cron sources without the explicit BDH opt-in marker;
- turns with unresolved vault scope;
- turns with empty user or assistant content.

A late completion callback cannot resurrect a session that has already been
finalized.

## Vault routing and isolation

Synthesis uses deterministic scope captured for the turn write path. Explicit vault hints, structured scope-map identities, stable platform identities and permitted unscoped default form the authority chain. Declared unresolved scopes fail closed. Mixed-vault buffers are rejected.

The semantic overlay in `vault_router.py` is **retrieval-only**. It can select a vault for a read when deterministic scope has no result, but that result is deliberately NOT saved into write state or synthesis buffers. A topic match is not write permission. Standalone watchers apply an additional actor-policy gate from `synthesis_scope.py`; do not conflate it with hook routing. See [operations](operations.md) and [semantic router](semantic-vault-router.md).

## Synthesis request

The bridge sends a single asynchronous BDH request with:

```json
{
  "query": "Synthesis of an entire agent session...",
  "user_prompt": "USER: ...\nASSISTANT: ...",
  "source": "session_synthesis",
  "vault_id": "<resolved vault, when scoped>",
  "metadata": {
    "synthesis_id": "<uuid4>",
    "session_id": "<session-id>",
    "queued_at": 1700000000.0,
    "transcript_sha256": "<sha256-hex>"
  }
}
```

The request uses the normal BDH write path. BDH's existing durable/neurogenesis
gates decide whether the extracted material is worth storing. The bridge does
not blindly materialize the transcript as a note and does not inject the
synthesis into the user's current answer.

The request is fire-and-forget and bounded. If BDH is unavailable, the hook
logs the failure and the Hermes conversation continues normally.

## Audit metadata

Each synthesis request carries structured metadata so downstream audit
consumers can correlate requests and verify transcript integrity without
storing the raw transcript in audit records.

The metadata dict contains:

| Field | Type | Description |
|---|---|---|
| `synthesis_id` | `str` | UUID4 unique to this synthesis flush |
| `session_id` | `str` | The Hermes session that produced this synthesis |
| `queued_at` | `float` | Unix timestamp when the request was queued |
| `transcript_sha256` | `str` | SHA-256 hex digest of the bounded transcript |
| `accepted_count` | `int` | Turns already written directly to BDH |
| `context_only_count` | `int` | Turns retained as context without a direct write |

The raw transcript never enters the audit record — only its hash, which
lets downstream consumers verify transcript integrity without re-deriving
content. The bridge never reads or logs the metadata dict; it is forwarded
verbatim to BDH.

## Configuration

The feature is opt-in:

| Environment variable | Default | Meaning |
|---|---:|---|
| `BDH_SESSION_SYNTH_ENABLED` | `false` | Enable session-end synthesis |
| `BDH_SESSION_SYNTH_MIN_TURNS` | `1` | Minimum eligible buffered turns (accepted or context-only) |
| `BDH_SESSION_SYNTH_MAX_CHARS` | `20000` | Maximum transcript characters; provider is operator-selected |
| `BDH_SESSION_TURN_USER_MAX_CHARS` | `4000` | Per-turn user-text cap |
| `BDH_SESSION_TURN_ASSISTANT_MAX_CHARS` | `12000` | Per-turn assistant-text cap |
| `BDH_SESSION_SYNTH_TIMEOUT` | `300` | Request timeout seconds, clamped 60–600 |

Recommended rollout:

1. enable the flag in a local/operator environment;
2. keep the default minimum and transcript cap initially;
3. verify the resolved vault and `source=session_synthesis` in logs/telemetry;
4. review generated concepts for durability and scope correctness;
5. only then consider changing thresholds.

The source override is configured in the local BDH runtime config under
`llm_source_overrides.session_synthesis`; the public `bdh-config.yaml` contains
only a commented generic example. Private vault mappings and credentials never
belong in the repository.

## Which LLM is used?

There are three distinct pieces of logic; they must not be conflated:

### 1. Vault router: no LLM

`vault_router.py` is a local deterministic overlay. It scores the query against
an operator-maintained index of vault titles and concepts. It does not call a
model.

### 2. Session synthesis: source-specific BDH runtime LLM

The bridge does not run a separate synthesis model. It sends the bounded
transcript to BDH's `/api/query` write path with `source=session_synthesis`.
BDH resolves a source-specific runtime override before generating the response
and extracting durable concepts.

No local provider/model is hard-coded for this source. Harness merges the operator's `llm_source_overrides.session_synthesis` into the selected runtime and disables completion fallback for synthesis sources. A configured cloud primary remains cloud; disabling fallback is not a local-only guarantee.

To require local completion, set `local_only: true` with a local `ollama` or `omlx` provider and an actual served model/loopback `base_url` in the Harness override. [Operations](operations.md) gives a portable example. Embedding routing and rewrite classification must be reviewed separately. The synthesis audit is per-vault `.bdh-audit/synthesis.jsonl`; transcript hashes there do not remove raw text from Hermes DBs, durable buffers, payloads or model input. The final storage decision belongs to Harness staging/neurogenesis gates.

### 3. Optional `pre_llm_call` rewrite/classification: separate model

If `BDH_QUERY_REWRITE_ENABLED=true`, the bridge makes a separate preprocessing
call before retrieval. Its primary model is configured by `BDH_REWRITE_MODEL`,
currently defaulting to:

```text
model: deepseek-v4-flash:cloud
provider: Ollama Cloud
```

That model classifies retrieval/storage eligibility and can produce a
retrieval-only rewrite. The rewrite fallback chain is documented in the main
README and is independent of the source-specific synthesis model.

## Safety properties and limitations

- **No prompt-path regression:** synthesis is asynchronous and never blocks the
  current answer.
- **Buffer categories:** accepted turns follow successful per-turn writes; safe context-only turns can also enter the buffer without a direct write.
- **Scope isolation:** mixed-vault sessions are rejected.
- **Bounded memory:** buffer and transcript limits prevent unbounded process or
  request growth.
- **No ambiguous replay:** the synthesis POST is retried only when BDH provably never received it (e.g. connection refused). A timeout while BDH is generating, a reset after sending or an error response is never retried, so a slow local model cannot cause a second synthesis. Do not replay ambiguous writes by hand either; see [operations](operations.md).
- **Opt-in:** the feature is disabled unless explicitly enabled.
- **Durable buffer:** when synthesis is enabled and the watcher starts, the bridge loads/persists `bdh-session-synthesis-buffer.json` (override `BDH_SESSION_SYNTH_BUFFER_FILE`) under the selected Hermes home. Treat it as private transcript data; failed persistence and crash windows are not an exactly-once guarantee.
- **Not a replacement for curation:** synthesis is a candidate learning path,
  not an authoritative decision ledger.
- **Privacy boundary:** the transcript is sent through the configured BDH
  runtime path. Do not enable it for a scope whose data-handling policy has not
  been approved.

## Verification

The bridge test suite covers:

- buffering only after successful writes;
- finalization and reset flushing;
- pending asynchronous writes;
- short-session skipping;
- interleaved sessions;
- mixed-vault rejection;
- idle flush: exactly one synthesis per epoch, idempotent repeated idle,
  new epoch on activity, finalize-after-idle without duplication, pending-write
  barrier, mixed-scope rejection, and below-min-turns non-destructive retention;
- deterministic turn-scope propagation and retrieval-only semantic hints;
- audit metadata: synthesis_id, session_id, queued_at, transcript_sha256.

Run locally with the feature enabled but the router index unset when testing
configuration-independent behavior:

```bash
env -u BDH_VAULT_ROUTER_INDEX -u BDH_VAULT_ID \
  BDH_SESSION_SYNTH_ENABLED=true \
  python3 -m pytest -q -o addopts=
```

For an actual operator rollout, inspect the resulting BDH response and
telemetry rather than treating a queued asynchronous request as proof that a
new concept was stored.
