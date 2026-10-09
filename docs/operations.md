# Bridge operations and privacy

## Read, write and synthesis are different paths

| Path | Behavior |
|---|---|
| `bdh_stats` | GET graph metrics, read-only |
| `pre_llm_call` | Optional automatic retrieval with `learn:false`; may invoke rewrite LLM if enabled |
| `bdh_query` | On-demand normal Harness query; learning/response generation are not disabled by its schema |
| `post_api_request` | Eligible completed turns submitted asynchronously with `source:assistant_response` |
| In-process session synthesis | Optional bounded buffer + lifecycle/idle flush with `source:session_synthesis` |
| Standalone watchers | Read session/room DBs, apply actor policy, submit bounded synthesis |

There is **no universal automatic-write-disable flag**. `BDH_QUERY_REWRITE_ENABLED=false` disables classification/rewrite, not the entire post-response write path: a captured turn without `store_candidate=false` can still be submitted. `BDH_SESSION_SYNTH_ENABLED=false` disables the optional in-process synthesis, not tools or per-turn writes. To verify without writes, do not run conversation hooks; use isolated discovery and stats as in [quickstart](quickstart.md). To disable the plugin's behavior in a live profile, remove it from that profile's enabled list and restart only its owning process after approval.

## Scope authority

- **Deterministic hook scope** resolves explicit vault hints, structured scope-map identities, stable platform identities and permitted unscoped default. Unresolved declared scope is not silently routed elsewhere.
- **Semantic overlay** uses a local concept index for retrieval only. Its vault selection is NOT saved into write state or synthesis buffers. Topic relevance is not permission to write.
- **Buffered synthesis** uses deterministic captured turn scope and rejects mixed-vault buffers.
- **Standalone watchers** use `synthesis_scope.py` actor policy. Addressed handles, profile identity and room registry must agree. Unknown or contradictory actor signals are rejected; default-profile sessions require the explicit policy gate and verified default DB/profile/source checks. Topic words alone never authorize synthesis.

The actor gate applies to the standalone watcher workflow; it must not be described as a universal substitute for hook/tool configuration. Private scope maps/indexes are operator data. See [semantic router](semantic-vault-router.md) and [deployment](../deploy/README.md).

## Data leaving the process

`BDH_API_URL` receives queries, eligible response text and synthesis transcripts. Harness selects its own provider (global/per-vault/source override). Synthesis sources disable completion fallback, **but can still select a cloud primary**. The bridge does not hard-force oMLX or local-only synthesis.

If local completion is required, configure the actual Harness runtime, for example:

```yaml
llm_source_overrides:
  session_synthesis:
    provider: ollama
    model: qwen3:0.6b  # choose/pull a suitable local model; this is only a demo
    base_url: http://127.0.0.1:11434
    local_only: true
```

Set `room_synthesis` separately when needed, and verify embeddings URL too. oMLX is optional, not required: configure `provider: omlx`, the actual served model and local `base_url` explicitly.

Rewrite/classification is a separate bridge model chain and may send current user text plus conversation context to cloud services if enabled. Do not infer its privacy from a local synthesis override. Credentials belong in protected environment/secret storage, not versioned YAML or examples.

Synthesis audit hashes do not mean there is no raw transcript anywhere: Hermes DBs, durable bridge buffers, payloads, generated candidates/notes and selected provider input can contain sensitive text. Define retention/access rules for all stores. No universal automatic retention cleanup is promised.

## Dry-run and ledgers

| Workflow | Dry-run effect |
|---|---|
| `session_synthesis_watcher.py --check --check-actor-handle … --expect-vault-id …` | Validates SessionDB integrity/schema, policy and exact actor→vault route; no message bodies, POST, state-file or ledger writes. A valid empty workload passes. |
| `session_synthesis_watcher.py --dry-run --backlog-once` | Reconstructs approved idle-session transcripts in memory and prints prospective scope; returns before POST and before session-ledger record |
| `room_synthesis_watcher.py --dry-run` | Does not POST, **but records the content digest in its configured ledger** |
| In-process idle/finalize synthesis | Not a watcher dry-run; uses epoch/buffer guards and durable session buffer |

Both standalone watchers use persistent ledgers in normal operation. Session ledger defaults to `bdh-session-synthesis-ledger.json` under the selected Hermes home; room ledger defaults to `bdh-synthesis-ledger.json`. Override them with `BDH_SESSION_SYNTHESIS_LEDGER_FILE` and `BDH_SYNTHESIS_LEDGER_FILE` respectively.

For any dry-run, use a copied test DB/policy and explicit disposable ledger paths. Room dry-run on a production ledger can suppress a later real submission. Use a **new scratch ledger path** to repeat inspection, never delete a whole production ledger as a convenience.

```bash
# After exporting a scratch HERMES_HOME with copied test DB/policy:
export BDH_SESSION_SYNTHESIS_LEDGER_FILE="$HERMES_HOME/session-dryrun.json"
export BDH_SYNTHESIS_LEDGER_FILE="$HERMES_HOME/room-dryrun.json"
python session_synthesis_watcher.py --dry-run --backlog-once --backlog-limit 2
python room_synthesis_watcher.py --dry-run --backlog-once --backlog-limit 2
```

These are advanced commands; missing/invalid actor policy should result in skipped work, not a guessed vault. Review printed scope and use a bounded backlog. Neither "queued" nor a ledger row proves a concept was applied—read the Harness audit/candidate/operation records.

## Timeouts, recovery and updates

Do not blindly replay a non-idempotent query POST after an ambiguous timeout. Read back exact synthesis correlation/candidate records. A client error can coexist with a server-side write. Synthesis has its own bounded timeout (`BDH_SESSION_SYNTH_TIMEOUT`: default 300s, clamped 60–600); it is separate from rewrite budget (default/max 15s).

For recovery, back up ledgers and durable buffers first; inspect bounded backlog in an isolated copy; verify outcomes in `.bdh-audit/synthesis.jsonl` and candidate/operation state. A persisted live→idle transition is not automatically retried if its callback fails; after checking audit for an ambiguous prior POST, use only an explicitly approved bounded recovery. Standalone digest deduplication and in-process epoch guards are distinct and do not cross-deduplicate; prefer one synthesis producer per session scope or review candidate correlation for duplicates. Do not run competing standalone watcher processes against the same ledger.

For backup, stop all relevant writers and preserve the profile's DBs, durable synthesis buffer, idle state, both ledgers, operator policies/indexes/config and the consistent Harness vault/cache/audit snapshot. Protect them as private data. A repo archive is only a code backup.

For stop/update/uninstall, identify the owning profile/process first. Export changes or `git pull` do not hot-reload a running plugin. Update a test checkout, verify stats/tests, then deliberately restart the owning test process; production rollout requires approval. Remove the enabled entry/plugin directory to uninstall, but retain vault/DB/audit data unless explicitly requested. No core patch is necessary.
