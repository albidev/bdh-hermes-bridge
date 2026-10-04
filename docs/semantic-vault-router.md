# Semantic vault router: retrieval only

An optional deterministic lexical overlay suggests a vault from query text using a local concept index. It does not call an LLM. Its output is a **read hint**, never write authorization.

## Resolution and authority

`pre_llm_call` first resolves deterministic hook scope: explicit vault hints, structured scope map, stable platform identities and the permitted unscoped default. An unresolved declared scope is rejected. Only when deterministic routing has no vault does the semantic overlay run.

A unique confident match is used for that retrieval request. **It is deliberately not persisted into turn state**, so per-turn writes and buffered synthesis keep their deterministic scope. A query mentioning a client can read that client's relevant notes without granting authority to store the conversation in that client vault. Confidence/margin checks reduce ambiguous suggestions; they are not an access-control system.

The standalone watcher actor policy is separate: addressed handles, serving profile, room registry and verified default-session opt-in determine whether synthesis is authorized. Contradictory/unknown actors fail closed. See [operations](operations.md) and [watcher deployment](../deploy/README.md).

## Local index

Default file: `vault-router-index.local.json`; override with `BDH_VAULT_ROUTER_INDEX`. Keep it private and untracked: titles/concepts can disclose project information.

```bash
python3 scripts/build_vault_router_index.py \
  --vault /path/to/vault-a --vault-id a \
  --vault /path/to/vault-b --vault-id b \
  --output vault-router-index.local.json \
  --min-node-size 1024
```

Check actual IDs against your configured Harness vaults. The index is an optional retrieval aid, not a mandatory Mission Control registry. Explicit hints/session bindings are not overridden. A missing index, no adequate match or ambiguous scores means no semantic hint.

## Verification

Run `test_vault_router.py` in the repository's isolated test environment with ambient `BDH_VAULT_ROUTER_INDEX` unset; tests cover missing/invalid indexes, unique/ambiguous matching and confidence thresholds. The hook/write contract is covered separately in the bridge suite. Do not validate writes by querying a real client's vault.
