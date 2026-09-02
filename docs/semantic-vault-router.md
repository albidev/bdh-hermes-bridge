# Semantic Vault Router

Experimental local routing index for the BDH bridge. It suggests a `vault_id` from the user query when deterministic routing returns `None`.

## How it works

1. Deterministic routing resolves `vault_id` via explicit hint, session context, project/repo alias, or `BDH_VAULT_ID`.
2. If that returns `None`, the semantic overlay queries a local index of `{vault_id, title, concepts}`.
3. If a single vault is confidently matched, it is used as an implicit `vault_hint` for that turn.
4. If the query is ambiguous or no match passes the confidence threshold, the overlay is ignored and the query proceeds without a suggested vault.

## Index file

- Path: `vault-router-index.local.json`
- Never commit this file. It is gitignored.
- Regenerate it with `scripts/build_vault_router_index.py`.

## Bootstrap script

```bash
python3 scripts/build_vault_router_index.py \
  --vault /path/to/vault-a --vault-id a \
  --vault /path/to/vault-b --vault-id b \
  --output vault-router-index.local.json \
  --min-node-size 1024
```

## Rules

- The index is local only.
- Do not hardcode private vault names or paths in shared skills or docs.
- Deterministic routing remains the primary path; this overlay is optional and fallback-only.
