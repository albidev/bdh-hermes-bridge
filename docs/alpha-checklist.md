# Bridge alpha rollout checklist

Begin with [isolated onboarding](quickstart.md), [privacy/operations](operations.md) and the [compatibility matrix](compatibility.md). Do not adopt in a production chat merely because stats respond.

- [ ] Verify manifest/source version and actual installed Hermes/Python/dependency versions.
- [ ] Discover the plugin under separate `HOME`/`HERMES_HOME` and dispatch `bdh_stats` against a populated fictional Harness demo.
- [ ] Compare clean-checkout baseline and changed suite with ambient credentials/BDH variables removed.
- [ ] Verify deterministic hook routing, retrieval-only semantic hints and standalone watcher actor policy separately.
- [ ] Verify local-only Harness source overrides explicitly; fallback suppression alone does not prohibit cloud inference.
- [ ] Review rewrite provider data flow and all transcript stores/retention before enabling real turns.
- [x] Non-idempotent POSTs are never retried after an ambiguous outcome (all timeout shapes, reset after send, HTTP error, bad body); regression tests in `test_post_retry_safety.py`. This removes client double-writes; it is not an exactly-once guarantee.
- [ ] Test watcher dry-run with copied DB/policy and fresh scratch ledger paths; distinguish session no-record dry-run from room digest-recording dry-run.
- [ ] Back up buffers, ledgers, profile DBs/policy and consistent Harness state before a rollout.
- [ ] Enable one feature at a time in a disposable profile and read back outcomes from actual candidate/audit/operation records.
- [ ] Review changed artifacts independently, define release version/upgrade notes, and get approval before commit/push/tag or production restart.

The isolated discovery/stats path is usable without Mission Control or a production restart. **The automatic write path is not cleared for unattended production adoption until the intended profile E2E checks are resolved.** There is no universal automatic-write-disable switch; removing the plugin from the enabled profile and reloading its owner is different from toggling rewrite/synthesis.
