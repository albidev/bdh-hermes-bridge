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
  ├─ submit the normal per-turn write asynchronously
  └─ after a successful write, buffer the user/assistant pair + resolved vault
        │
        ▼
finalize / reset / session boundary
  ├─ wait logically for in-flight per-turn writes (without blocking the hook)
  ├─ discard short, failed, or mixed-scope buffers
  ├─ bound the transcript
  └─ submit one `source=session_synthesis` request to BDH
```

The current implementation uses Hermes `on_session_finalize` and
`on_session_reset` hooks. Session identity rotation remains supported as a
compatibility boundary, but a new turn is not required for a finalized session
to flush.

## What is buffered

A turn enters the synthesis buffer only after its normal BDH write request
succeeds. Each buffered item contains:

- the user message, capped at 1,500 characters;
- the assistant response, capped at 1,500 characters;
- the resolved `vault_id` for that turn.

The in-memory buffer is capped at 200 turns per session. The final transcript
is capped by `BDH_SESSION_SYNTH_MAX_CHARS` and is rendered as `USER:` /
`ASSISTANT:` pairs.

Failed per-turn writes never enter the buffer. A late completion callback cannot
resurrect a session that has already been finalized.

## Vault routing and isolation

The synthesis uses the same vault decision produced for the turn write path.
This is important: the semantic router must not affect only retrieval while the
later write silently falls back to the default vault.

Resolution order for each turn is:

1. explicit `vault_id` / `bdh_vault_id`;
2. structured project/client/workspace scope through the configured scope map;
3. stable platform identity through the configured scope map;
4. the local semantic vault-router overlay when deterministic routing has no
   result;
5. BDH's configured default when no scoped decision exists.

The bridge retains the resolved vault with every successful buffered turn. If a
session contains turns from more than one vault, the synthesis is rejected
rather than merged. This prevents client or project knowledge from crossing a
vault boundary.

The local semantic overlay is implemented by `vault_router.py`. It reads the
operator-maintained `BDH_VAULT_ROUTER_INDEX` and applies confidence and margin
guards. The index is local and ignored by Git; it must never be committed to
this public repository.

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

The raw transcript never enters the audit record — only its hash, which
lets downstream consumers verify transcript integrity without re-deriving
content. The bridge never reads or logs the metadata dict; it is forwarded
verbatim to BDH.

## Configuration

The feature is opt-in:

| Environment variable | Default | Meaning |
|---|---:|---|
| `BDH_SESSION_SYNTH_ENABLED` | `false` | Enable session-end synthesis |
| `BDH_SESSION_SYNTH_MIN_TURNS` | `3` | Minimum successful written turns |
| `BDH_SESSION_SYNTH_MAX_CHARS` | `6000` | Maximum transcript characters |

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

The current runtime for `session_synthesis` is deliberately local-only:

```text
provider:             oMLX
model:                qwen3.8-27b-oq4e-mtp
endpoint:             http://127.0.0.1:8083/v1/chat/completions
chat_template_kwargs: {enable_thinking: false, thinking: false}
fallbacks:            none
```

BDH hard-forces this source-specific route even if an older private config still
contains a Cloud override. The normal global BDH model and fallback chain remain
unchanged. The synthesis audit is persisted per vault at
`.bdh-audit/synthesis.jsonl`; it stores metadata and hashes, never the raw
transcript.
The final durable-storage decision remains BDH's neurogenesis/durability gate.

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
- **No failed-write learning:** only successfully submitted per-turn writes are
  buffered.
- **Scope isolation:** mixed-vault sessions are rejected.
- **Bounded memory:** buffer and transcript limits prevent unbounded process or
  request growth.
- **No duplicate retry learning:** non-idempotent BDH writes do not retry after
  a timeout that may have been processed server-side.
- **Opt-in:** the feature is disabled unless explicitly enabled.
- **Process-local buffer:** an abrupt process crash before finalization loses
  the unflushed in-memory buffer; prior successful per-turn writes remain
  independent.
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
- propagation of the semantic router's vault decision through retrieval,
  per-turn write, and session synthesis;
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
