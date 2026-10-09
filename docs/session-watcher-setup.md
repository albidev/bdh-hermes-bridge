# Standalone session-synthesis watcher: operator setup

This runbook is for an operator or setup agent installing the **standalone session watcher** on macOS. It is deliberately separate from enabling the bridge plugin in a chat gateway.

The watcher reads Hermes session databases read-only, waits for eligible TUI/Mission Control sessions to become idle, reconstructs bounded user/assistant turns, authorizes a destination vault from actor policy, and submits one `source=session_synthesis` request to BDH. It does not edit Hermes core. With Harness staging enabled, the request becomes a review candidate rather than an automatically published Markdown note.

> **Do not begin by loading the LaunchAgent.** First establish the exact Hermes home, vault ID, authorization policy, Harness staging behavior, and the expected data flow. A missing or ambiguous authorization must mean “skip synthesis.”

## 1. Distinguish the three mechanisms

There are three related but independent paths:

| Mechanism | Trigger | Reads from | Enablement |
|---|---|---|---|
| Bridge per-turn hooks | Each agent API response | Current hook arguments | Plugin enabled in an owning Hermes process; may write independently of rewrite/synthesis flags |
| Bridge in-process session synthesis | Hermes finalize/reset and bridge-owned idle watcher | Bridge's buffered turns | `BDH_SESSION_SYNTH_ENABLED=true` in the owning process |
| **Standalone session watcher (this runbook)** | SessionDB live→idle transition, plus bounded startup backlog | Hermes `state.db` and profile DBs | launchd agent + `BDH_SESSION_SYNTH_ENABLED=true` + a valid actor policy |

Do not enable the plugin or turn on conversation hooks merely to test the standalone watcher. Conversely, setting `BDH_SESSION_SYNTH_ENABLED=true` in a shell does not start a daemon. The LaunchAgent receives its own environment and must be bootstrapped into the correct logged-in user session.

The standalone session watcher handles TUI and Mission Control 1:1 sessions. Hosted rooms use the separate room watcher and room registry; do not point both at the same ledger.

## 2. Agent procedure and stop gates

The setup agent should complete these in order and stop rather than guess whenever an item is unresolved:

1. **Discover, do not mutate:** identify macOS, the intended Hermes home/profile, the actual SessionDB path, bridge checkout, Python interpreter, Harness URL, configured vault IDs, staging flag, and current LaunchAgent state. Do not print secrets.
2. **Get explicit operator decisions:** which actor/session is authorized, exact destination `vault_id`, whether a small bounded backlog may be submitted at startup, and whether transcript text may be sent to the configured Harness/model providers.
3. **Prepare local policy:** map an addressed `@handle` to the exact vault ID, or explicitly opt the verified default-profile TUI/Mission Control sessions into the special `core` vault. Do not infer a destination from topic words or vault names.
4. **Verify staging and provider policy:** if the requirement is “no automatic Markdown publication,” confirm `session_synthesis_staging_enabled: true` for the target vault in the running Harness. Separately inspect all other enabled write paths; this setting only gates session-synthesis application.
5. **Run a dry-run:** use the real intended policy and SessionDB but `--dry-run --backlog-once` with a bounded limit. Review every printed destination/count. Dry-run must not POST or update the session watcher ledger.
6. **Render and validate the LaunchAgent:** replace every template placeholder with verified absolute paths; validate the resulting plist before bootstrap.
7. **Activate only after approval:** bootstrap only `ai.bdh.session-synthesis-watcher`; do not restart a Hermes gateway or Harness for this step.
8. **Verify live behavior:** verify the label/process, logs, authorized vault, audit correlation and pending-review candidate. A PID or ledger row alone is not proof that a candidate was staged.
9. **Report the open state:** record whether backlog recovery was enabled, which vault was authorized (ID only), checks performed, and remaining limitations. Never report a candidate as published unless Markdown was read back and verified.

## 3. Prerequisites and read-only discovery

Required:

- macOS with `launchd` and a logged-in GUI user session;
- a local checkout of `bdh-hermes-bridge` containing `session_synthesis_watcher.py` and `deploy/ai.bdh.session-synthesis-watcher.plist`;
- a Python interpreter compatible with the installed Hermes package, with the Hermes SessionDB modules importable;
- the correct `HERMES_HOME` for the profile whose sessions are in scope;
- the BDH HTTP API reachable at the chosen `BDH_API_URL`;
- a local, gitignored `synthesis-policy.local.json` authorizing a specific actor-to-vault route;
- a Harness vault configured for the intended synthesis behavior.

Before writing policy or loading a daemon, inspect only the relevant values:

```bash
printf 'HERMES_HOME=%s\n' "$HERMES_HOME"
test -f "$HERMES_HOME/state.db" && echo 'default SessionDB exists'
test -x "$PYTHON" && "$PYTHON" -c 'import hermes_cli; print("Hermes Python imports OK")'
curl --fail --silent --show-error "$BDH_API_URL/health"
curl --fail --silent --show-error "$BDH_API_URL/api/vaults"
```

If profiles are used, inspect `$HERMES_HOME/profiles/*/state.db` as well. The watcher discovers profile databases below the selected Hermes home. Do not use a different user's home or a production DB just because it is convenient. Do not dump session contents into logs or chat.

The watcher’s default inputs are `$HERMES_HOME/state.db` and `$HERMES_HOME/bdh-session-synthesis-watcher.json`; it opens SessionDB read-only. It considers session sources `tui` and `mission-control`, reconstructs completed turns, ignores tool/system rows as separate turns, and skips truncated responses. It polls at the configured interval (60 seconds in the template); default idle threshold is 300 seconds, with a 30-second lower clamp.

## 4. Choose an authorization policy

The policy is local operator configuration, intentionally gitignored. The default path is `synthesis-policy.local.json` beside the bridge module; the LaunchAgent template sets `BDH_SYNTHESIS_POLICY_FILE` explicitly. An absent, invalid, or non-authorizing policy means no synthesis.

### Option A: explicitly addressed actor for a personal/project vault

For a 1:1 session, authorize the addressed `@handle` to exactly one configured Harness vault. Replace both example values with the literal handle used in Hermes and the exact ID returned by `/api/vaults`:

```json
{
  "version": 1,
  "mention_prefixes": {
    "ASTERION_HANDLE": "EXACT_VAULT_ID"
  },
  "allow_default_core_sessions": false
}
```

The session must contain the addressed handle in a user message for the actor gate to authorize the synthesis. The mapping is based on the explicit handle, not on prose about a client/project. Unknown, ambiguous, or conflicting handles fail closed. Do not put private transcript text, credentials, or unrelated mappings in this file.

### Option B: explicitly authorize default-profile sessions to Core

This special case is only for a session verified by the watcher to be in the selected default `state.db`, with `profile_name=default` and source `tui` or `mission-control`:

```json
{
  "version": 1,
  "allow_default_core_sessions": true
}
```

The destination is the literal vault ID `core`; this flag does not authorize arbitrary vaults, secondary profile DBs, rooms, cron sessions, or unknown/ambiguous `@handles`. Do not use it as a generic “allow all sessions” switch.

For other session/room authorization patterns, inspect `synthesis_scope.py` and `deploy/README.md`; do not assume `profile_vaults` grants arbitrary 1:1 sessions. The actor resolver is intentionally stricter than retrieval routing.

Store policy as UTF-8 JSON, mode-restrict it to the operator, and keep it out of version control. Verify it parses and resolves the intended test session before activating the service. Never add production transcript text to test fixtures.

## 5. Configure the Harness before enabling writes

A standalone watcher sends a normal BDH synthesis request. The watcher itself does not decide whether to publish note files; Harness configuration owns that gate.

For a review-only rollout:

```yaml
session_synthesis_staging_enabled: true
```

Confirm this is enabled for the target deployment and that the candidate-review path is available. If staging is off, or the deployed Harness is older and ignores the option, do not assume the watcher is review-only. Validate the running build/config before sending real session content.

If the requirement is no automatic Markdown publication by **any** path, also audit per-turn bridge writes, `bdh_query`, Harness neurogenesis, external ingest, and any scheduled synthesis/consolidation jobs. Session staging only covers the session-synthesis apply path. A watcher daemon can be enabled without enabling the bridge plugin, but its submitted synthesis still uses the Harness's configured embedding and completion routes.

If transcript/model processing must stay local, configure the Harness source override explicitly, e.g. `llm_source_overrides.session_synthesis` with a local `ollama` or `omlx` provider, a real served model, loopback `base_url`, `local_only: true`, and no fallbacks. Verify embeddings separately. Do not infer locality from a missing fallback or from `BDH_API_URL` being localhost: the Harness may call a remote model provider.

## 6. Dry-run before activation

Use a bounded backlog dry-run first. It reads and reconstructs eligible sessions, prints the resolved vault ID, turn count and digest prefix, but does **not** POST or record the session digest. Use a small limit; the normal startup backlog is bounded to three by default.

```bash
cd "$REPO"
env HERMES_HOME="$HERMES_HOME" \
  BDH_API_URL="$BDH_API_URL" \
  BDH_SYNTHESIS_POLICY_FILE="$REPO/synthesis-policy.local.json" \
  BDH_SESSION_SYNTH_ENABLED=true \
  PYTHONPATH="$REPO" PYTHONUNBUFFERED=1 \
  "$PYTHON" session_synthesis_watcher.py \
  --dry-run --backlog-once --backlog-limit 3
```

Expected outcome: only explicitly authorized sessions appear, each with the intended vault ID; no unauthorized/default-fallback session appears. Any unexpected target, missing policy, ambiguous actor, unreadable SessionDB, or output containing data the operator did not approve is a **stop condition**. No POST should occur in dry-run mode.

For a fresh install where no historical session should be considered, first dry-run with `--backlog-limit 0`. The live idle trigger only handles observed live→idle transitions; already-idle sessions need a later explicitly approved bounded backlog pass to be recovered.

## 7. Render and install only the session LaunchAgent

The committed plist is a template, not an installable file. Render absolute paths for the chosen checkout, Hermes home and interpreter. The deployment guide has a shell template for both watchers; this example renders **only the session watcher** and avoids shell substitution problems with paths:

```bash
export REPO="$HOME/Projects/bdh-hermes-bridge"
export HERMES_HOME="$HOME/.hermes"
export PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"
export PLIST_OUT="$HOME/Library/LaunchAgents/ai.bdh.session-synthesis-watcher.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HERMES_HOME/logs"

python3 - <<'PY'
import os
from pathlib import Path
src = Path(os.environ["REPO"]) / "deploy/ai.bdh.session-synthesis-watcher.plist"
out = Path(os.environ["PLIST_OUT"])
text = src.read_text(encoding="utf-8")
for key, value in {
    "__PYTHON__": os.environ["PYTHON"],
    "__REPO__": os.environ["REPO"],
    "__HERMES_HOME__": os.environ["HERMES_HOME"],
}.items():
    text = text.replace(key, value)
if "__" in text:
    raise SystemExit("unrendered template placeholder remains")
out.write_text(text, encoding="utf-8")
PY
plutil -lint "$PLIST_OUT"
```

Before `bootstrap`, read the rendered plist and verify every executable/path/env value. It must include the intended `HERMES_HOME`, `PYTHONPATH`, `BDH_API_URL`, `BDH_SYNTHESIS_POLICY_FILE`, `BDH_SESSION_SYNTH_ENABLED=true` and a **session-only** `BDH_SESSION_SYNTHESIS_LEDGER_FILE`. Keep the session and room watcher ledgers separate.

The template performs a bounded startup backlog of up to three sessions. For a no-backlog first activation, add these two arguments to `ProgramArguments` in the **rendered local plist** before the interval:

```xml
<string>--backlog-limit</string>
<string>0</string>
```

Do not edit the committed template merely to make a local activation. Validate the rendered plist again after editing.

After review and explicit operator approval, load only this service:

```bash
launchctl bootstrap "gui/$(id -u)" "$PLIST_OUT"
launchctl print "gui/$(id -u)/ai.bdh.session-synthesis-watcher"
```

A plist edit is not applied to an already loaded service. To reload it, boot out and bootstrap this exact label; do not restart the Hermes gateway or BDH Harness for a plist-only change.

## 8. Verify operation and candidate staging

Verify each layer independently:

1. **launchd:** `launchctl print gui/$(id -u)/ai.bdh.session-synthesis-watcher` shows the expected program and environment. Check the process and the configured stdout/stderr logs.
2. **Policy:** logs show unauthorized sessions skipped; there must be no fallback to the Harness default vault for an unauthorized session.
3. **Harness:** confirm `session_synthesis_staging_enabled` for that vault before a real request. Check health/stats and the candidate endpoint without exposing note or transcript text.
4. **After an approved test session becomes idle:** check Harness synthesis audit and the exact candidate's status/source/vault. Expect `source=session_synthesis` and `pending_review`; a candidate is not yet a published note.
5. **No Markdown publication:** compare the target vault Markdown inventory/hashes before and after the test. Candidate staging may create operational JSON under the staging directory; it must not change curated `.md` notes.
6. **Learning and indexes:** inspect the relevant state/counters separately. A successful POST, audit row, candidate, Hebbian update and Markdown write are distinct outcomes.

A watcher PID, clean log, incremented ledger, HTTP 200, or non-empty synthesis response alone does not prove correct scope or candidate staging. Read back the exact authorized candidate and intended Markdown target before any approval/merge.

The watcher idle detector is transition-based. It notices a live session becoming idle while the watcher is running. The startup backlog is a separate bounded recovery path for sessions already idle before startup. `--once` only scans for transitions; to inspect already-idle sessions use `--dry-run --backlog-once` with a fresh review and a small limit.

## 9. Stop, disable, rollback

Disable only the watcher agent:

```bash
launchctl bootout "gui/$(id -u)/ai.bdh.session-synthesis-watcher"
```

For permanent disablement, remove the local plist after bootout and set `allow_default_core_sessions` false / remove the actor mapping in the local policy. Do not delete the SessionDB, vault, audit files, staging candidates or ledgers as part of disablement. Preserve them unless separately asked to remove data.

For rollback after an unexpected candidate or write: stop the watcher first; preserve logs, policy, ledger, Harness audit and candidate JSON; identify whether any Markdown changed; back up the exact affected files before a reviewed revert. Do not replay a possibly accepted synthesis POST after a timeout: it is non-idempotent.

## 10. Troubleshooting

| Symptom | Likely check |
|---|---|
| Agent is running but no synthesis occurs | Policy absent/invalid, session source not `tui`/`mission-control`, no observed live→idle transition, below `BDH_SESSION_SYNTH_MIN_TURNS`, unresolved actor/vault, or session already recorded in ledger. Try bounded dry-run backlog. |
| Default profile session is skipped | Expected unless the verified default DB + profile/source gate is satisfied and `allow_default_core_sessions: true` is explicitly configured. |
| A client/project 1:1 session is skipped | Add an explicit addressed `@handle` → exact vault mapping; do not authorize by topic or assume profile identity is enough. |
| Wrong vault appears in dry-run | Stop. Correct the policy/handle/vault ID; do not bootstrap. |
| Process exits/restarts repeatedly | Check `launchctl print`, plist lint, executable/imports, HOME/PYTHONPATH, file permissions and stderr log. |
| HTTP/API failure | Check the exact `BDH_API_URL`, Harness `/health`, selected vault, and provider availability. Do not replay ambiguous POSTs. |
| Candidate not visible | Confirm request reached Harness audit, staging is enabled on the target vault, and use the corresponding `synthesis_id`/session correlation. A ledger row alone is insufficient. |
| Dry-run appears to submit | Confirm `--dry-run` is present and use the session watcher (not the room watcher); session dry-run returns before POST and ledger recording. |
| Old sessions not seen | Live→idle trigger cannot observe an already-idle session. Run a reviewed bounded `--dry-run --backlog-once`, then an explicitly approved bounded recovery if needed. |

## 11. Data handling and limits

The watcher opens Hermes SessionDB read-only, but it reconstructs raw user/assistant text in memory and submits a bounded transcript to the configured Harness endpoint. The selected Harness completion provider may be local or cloud; embeddings have their own provider path. Transcript hashes and audit metadata do not make the transcript anonymous or remove it from Hermes databases, provider requests, candidate staging, or approved notes.

The session watcher has its own digest ledger (`BDH_SESSION_SYNTHESIS_LEDGER_FILE`, default `$HERMES_HOME/bdh-session-synthesis-ledger.json`) and idle state file (`$HERMES_HOME/bdh-session-synthesis-watcher.json`). The room watcher has different ledgers. Do not share ledger files between processes: each process caches and rewrites its ledger. Back up policy, state, ledger, SessionDB and relevant Harness audit/candidate/vault data before destructive maintenance.

## References

- [Deployment templates and room watcher](../deploy/README.md)
- [Session synthesis architecture and limits](session-synthesis.md)
- [Privacy, dry-run and operations](operations.md)
- [Isolated onboarding](quickstart.md)
- [Actor authorization implementation](../synthesis_scope.py)
- [Standalone watcher implementation](../session_synthesis_watcher.py)
