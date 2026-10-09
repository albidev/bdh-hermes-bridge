# Standalone session-synthesis watcher: operator setup

This runbook is for an operator or setup agent installing the **standalone session watcher** on macOS or Linux. It is deliberately separate from enabling the bridge plugin in a chat gateway.

The watcher reads Hermes session databases read-only, waits for eligible TUI/Mission Control sessions to become idle, reconstructs bounded user/assistant turns, authorizes a destination vault from actor policy, and submits one `source=session_synthesis` request to BDH. It does not edit Hermes core. With Harness staging enabled, the request becomes a review candidate rather than an automatically published Markdown note.

> **Do not begin by loading the LaunchAgent.** First establish the exact Hermes home, vault ID, authorization policy, Harness staging behavior, and the expected data flow. A missing or ambiguous authorization must mean “skip synthesis.”

## 1. Distinguish the three mechanisms

There are three related but independent paths:

| Mechanism | Trigger | Reads from | Enablement |
|---|---|---|---|
| Bridge per-turn hooks | Each agent API response | Current hook arguments | Plugin enabled in an owning Hermes process; may write independently of rewrite/synthesis flags |
| Bridge in-process session synthesis | Hermes finalize/reset and bridge-owned idle watcher | Bridge's buffered turns | `BDH_SESSION_SYNTH_ENABLED=true` in the owning process |
| **Standalone session watcher (this runbook)** | SessionDB live→idle transition, plus bounded startup backlog | Hermes `state.db` and profile DBs | launchd agent (macOS) or systemd user service (Linux) + `BDH_SESSION_SYNTH_ENABLED=true` + a valid actor policy |

Do not enable the plugin or turn on conversation hooks merely to test the standalone watcher. Conversely, setting `BDH_SESSION_SYNTH_ENABLED=true` in a shell does not start a daemon. The macOS LaunchAgent or Linux systemd user service has its own environment and must be activated for the correct OS user and Hermes home.

The standalone session watcher handles TUI and Mission Control 1:1 sessions. Hosted rooms use the separate room watcher and room registry; do not point both at the same ledger.

## 2. Agent procedure and stop gates

The setup agent should complete these in order and stop rather than guess whenever an item is unresolved:

1. **Discover, do not mutate:** identify whether the host is macOS or Linux, intended Hermes home/profile, actual SessionDB path, bridge checkout, Python interpreter, Harness URL and vault IDs, staging flag, active synthesis producers, and current launchd/systemd user-service state. Do not print secrets.
2. **Get explicit operator decisions:** which actor/session is authorized, exact destination `vault_id`, whether a bounded backlog may be submitted at startup, and whether transcript text may be sent to configured Harness/model providers.
3. **Resolve the destination before enabling:** for a 1:1 session use an explicitly addressed actor mapping to an existing exact vault ID; use default-profile opt-in only for literal `core`. If no `core` vault exists, stop and choose an addressed actor or provision/verify an explicitly approved `core` vault. Never substitute the Harness default or infer from topic.
4. **Verify staging and provider policy:** if the requirement is “no automatic Markdown publication,” confirm `session_synthesis_staging_enabled: true` for the target vault in the running Harness. Separately inspect all other enabled write paths; this setting only gates session-synthesis application.
5. **Run the non-writing preflight:** verify DB readability/schema and exact actor→vault resolution with `--check`; a valid DB with zero sessions is a pass, unlike a missing/invalid DB. No message bodies are read or emitted.
6. **Run an optional bounded dry-run:** use `--dry-run --backlog-once` only if the operator approves inspecting already-idle session transcripts. Review every printed vault/count; it must not POST or update the session ledger.
7. **Render and validate the platform service:** use launchd on macOS or the systemd user unit on Linux; replace/verify all paths before activation.
8. **Activate only after approval:** load/enable only `ai.bdh.session-synthesis-watcher`; do not restart a Hermes gateway or Harness for this step.
9. **Verify live behavior:** check service state, logs, authorized vault, audit correlation and pending-review candidate. A PID or ledger row alone is not proof that a candidate was staged.
10. **Report the open state:** record backlog policy, authorized vault ID, checks performed, active competing synthesis producers and remaining limitations. Never report a candidate as published unless Markdown was read back and verified.

## 3. Prerequisites and read-only discovery

Required:

- macOS with `launchd`, or Linux with `systemd` and a user manager;
- a local checkout of `bdh-hermes-bridge` containing `session_synthesis_watcher.py` and the relevant service template (`deploy/ai.bdh.session-synthesis-watcher.plist` on macOS or `deploy/bdh-session-synthesis-watcher.service` on Linux);
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

The policy is local operator configuration, intentionally gitignored. The service templates set `BDH_SYNTHESIS_POLICY_FILE` explicitly; if running manually, the default is `synthesis-policy.local.json` beside the bridge module. An absent, invalid, or non-authorizing policy means no synthesis.

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

The destination is the literal vault ID `core`. This opt-in works only if that exact vault is provisioned and returned by the live Harness `/api/vaults` response. If the deployment has no `core` ID, **do not** substitute the Harness default or another vault: either explicitly provision and verify an approved `core` vault, or use Option A to map an addressed actor to an existing vault. This flag does not authorize arbitrary vaults, secondary profile DBs, rooms, cron sessions, or unknown/ambiguous `@handles`. Do not use it as a generic “allow all sessions” switch.

For other session/room authorization patterns, inspect `synthesis_scope.py` and `deploy/README.md`; do not assume `profile_vaults` grants arbitrary 1:1 sessions. The actor resolver is intentionally stricter than retrieval routing.

Store policy as UTF-8 JSON, mode-restrict it to the operator, and keep it out of version control. Never add production transcript text to test fixtures.

### Non-writing setup preflight (required before service activation)

The preflight validates every discovered SessionDB read-only (SQLite integrity and required schema), reports a valid zero-session workload distinctly from a missing/invalid DB, loads the policy and checks an exact authorization route. It reads **no message bodies**, does not contact BDH, and does not create/update idle state or a digest ledger. Use the exact vault ID already verified from the running Harness `/api/vaults` response:

```bash
REPO="$HOME/Projects/bdh-hermes-bridge"
PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"  # use the installed Hermes interpreter
export HERMES_HOME="$HOME/.hermes"
export BDH_SYNTHESIS_POLICY_FILE="$REPO/synthesis-policy.local.json"
export PYTHONPATH="$REPO"

"$PYTHON" "$REPO/session_synthesis_watcher.py" \
  --check --check-actor-handle ASTERION_HANDLE \
  --expect-vault-id EXACT_VAULT_ID
```

For the special default-profile case, the explicit policy check is:

```bash
"$PYTHON" "$REPO/session_synthesis_watcher.py" \
  --check --check-default-core --expect-vault-id core
```

The second command verifies only the literal `core` policy route. The service still authorizes a real session only if it came from the verified default DB with `profile_name=default` and source `tui` or `mission-control`. Neither command processes history or posts anything. Exit `0` means DBs, policy and requested exact route passed; non-zero JSON output distinguishes DB/policy/authorization failures. Review the report and stop on any vault mismatch. This check proves the configured mapping, not that a future session contains the addressed handle; use the optional dry-run below when the operator separately approves inspection of an existing idle session.

A valid empty SessionDB is **not** an error: report `open_sessions: 0` and proceed only if the exact route check passes. Missing, unreadable, corrupt or schema-incompatible databases, absent/invalid policy, unresolved actor, or mismatched vault ID are stop conditions.


## 5. Configure the Harness before enabling writes

A standalone watcher sends a normal BDH synthesis request. The watcher itself does not decide whether to publish note files; Harness configuration owns that gate.

For a review-only rollout:

```yaml
session_synthesis_staging_enabled: true
```

Confirm this is enabled for the target deployment and that the candidate-review path is available. If staging is off, or the deployed Harness is older and ignores the option, do not assume the watcher is review-only. Validate the running build/config before sending real session content.

If the requirement is no automatic Markdown publication by **any** path, also audit per-turn bridge writes, `bdh_query`, Harness neurogenesis, external ingest, and any scheduled synthesis/consolidation jobs. Session staging only covers the session-synthesis apply path. A watcher daemon can be enabled without enabling the bridge plugin, but its submitted synthesis still uses the Harness's configured embedding and completion routes.

If transcript/model processing must stay local, configure the Harness source override explicitly, e.g. `llm_source_overrides.session_synthesis` with a local `ollama` or `omlx` provider, a real served model, loopback `base_url`, `local_only: true`, and no fallbacks. Verify embeddings separately. Do not infer locality from a missing fallback or from `BDH_API_URL` being localhost: the Harness may call a remote model provider.

## 6. Optional session dry-run (only with approved history inspection)

The setup preflight above is the positive non-writing test; do not use `--backlog-limit 0` as a substitute because it cannot select any session. If—and only if—the operator separately approves inspecting already-idle session history, use a bounded dry-run. It reconstructs transcripts in memory to print the resolved vault, turn count and digest prefix, but does **not** POST or record the session digest. Do not send the printed output if session IDs or vault metadata are sensitive.


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

Expected outcome: only explicitly authorized sessions appear, each with the intended vault ID; no unauthorized/default-fallback session appears. Any unexpected target, missing policy, ambiguous actor, unreadable SessionDB, or output containing data the operator did not approve is a **stop condition**. No POST or ledger write should occur in dry-run mode.



## 7. macOS: render and install the session LaunchAgent

The committed plist is a macOS template, not an installable file. Render absolute paths for the chosen checkout, Hermes home and interpreter. The deployment guide has a shell template for both watchers; this example renders **only the session watcher** and avoids shell substitution problems with paths:

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

Before `bootstrap`, read the rendered plist and verify every executable/path/env value. It must include the intended `HERMES_HOME`, `PYTHONPATH`, `PYTHONUNBUFFERED=1`, `BDH_API_URL`, `BDH_SYNTHESIS_POLICY_FILE`, `BDH_SESSION_SYNTH_ENABLED=true` and a **session-only** `BDH_SESSION_SYNTHESIS_LEDGER_FILE`. Keep the session and room watcher ledgers separate.

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

## 8. Linux: install the systemd user service

The Python watcher uses standard filesystem/SQLite/process APIs and has no macOS-only runtime dependency. On Linux, run it as the **same OS user that owns the intended Hermes home and SessionDB**, using a systemd *user* unit—not a root/system service. The repository ships `deploy/bdh-session-synthesis-watcher.service` as a starting template. Its paths assume the common layout below; edit every path if the checkout, Hermes home or interpreter differs:

- repository: `%h/Projects/bdh-hermes-bridge`;
- Hermes home: `%h/.hermes`;
- interpreter: `%h/.hermes/hermes-agent/venv/bin/python`;
- policy: repository `synthesis-policy.local.json`;
- BDH API: `http://127.0.0.1:8643`.

The template sets `--backlog-limit 0` so first activation does not submit old sessions. To activate recovery later, first inspect it with the dry-run command in section 6, then run an explicitly approved bounded one-shot with the same policy and ledger.

```bash
REPO="$HOME/Projects/bdh-hermes-bridge"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/bdh-session-synthesis-watcher.service"
mkdir -p "$UNIT_DIR" "$HOME/.hermes"
install -m 600 "$REPO/deploy/bdh-session-synthesis-watcher.service" "$UNIT"
```

Before enabling, edit the installed unit if any assumed path differs, and inspect the rendered values. Verify `PYTHONUNBUFFERED=1`, the explicit policy file, session-only ledger path, correct interpreter, and `--backlog-limit 0`. The unit runs unprivileged with `UMask=0077`; its output and INFO/WARNING/ERROR diagnostics go to the user journal without transcript bodies. Keep the session and room watcher ledgers distinct.

```bash
systemd-analyze --user verify "$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now bdh-session-synthesis-watcher.service
systemctl --user status bdh-session-synthesis-watcher.service --no-pager
journalctl --user -u bdh-session-synthesis-watcher.service -n 100 --no-pager
```

`systemctl --user enable --now` enables the unit for that user's systemd manager and starts it immediately; it does not require a system-wide install or root. By default the user manager may only be alive during a login session. Use `loginctl enable-linger "$USER"` only if the operator explicitly requires the watcher to keep running without an interactive login; this changes the user's service lifetime and must not be enabled implicitly.

To stop or disable only this watcher:

```bash
systemctl --user disable --now bdh-session-synthesis-watcher.service
```

After unit changes, run `systemctl --user daemon-reload` and restart this unit only after reviewing the diff. For logs use `journalctl --user -u bdh-session-synthesis-watcher.service`; Linux has no LaunchAgent plist/log paths. The bridge's Python watcher tests run in the repository's Ubuntu CI, but that is not an end-to-end test of a particular host's systemd manager, Hermes home, API or provider configuration.

## 9. Verify operation and candidate staging

Verify each layer independently:

1. **Service manager:** on macOS, `launchctl print` shows the expected program and environment; on Linux, `systemctl --user status` shows the correct active unit. Check the process and the configured stdout/stderr or journal logs.
2. **Policy and database observability:** standalone entrypoint logging defaults to INFO and writes transcript-free diagnostics to stderr/journal. Unauthorized actor skips are INFO; missing/invalid SessionDB reads are ERROR (once per distinct error state, with recovery logged). Distinguish a valid zero-session count from database failure in `--check` JSON output.
3. **Harness:** confirm `session_synthesis_staging_enabled` for that vault before a real request. Check health/stats and the candidate endpoint without exposing note or transcript text.
4. **After an approved test session becomes idle:** check Harness synthesis audit and the exact candidate's status/source/vault. Expect `source=session_synthesis` and `pending_review`; a candidate is not yet a published note.
5. **No Markdown publication:** compare the target vault Markdown inventory/hashes before and after the test. Candidate staging may create operational JSON under the staging directory; it must not change curated `.md` notes.
6. **Learning and indexes:** inspect the relevant state/counters separately. A successful POST, audit row, candidate, Hebbian update and Markdown write are distinct outcomes.

A watcher PID, clean log, incremented ledger, HTTP 200, or non-empty synthesis response alone does not prove correct scope or candidate staging. Read back the exact authorized candidate and intended Markdown target before any approval/merge.

The idle detector is transition-based: it notices a live session becoming idle while the watcher is running. It persists that transition **before** invoking the synthesis callback. If the callback fails, that same live→idle edge is not automatically retried after restart; this is not an exactly-once or eventual-delivery guarantee. Startup backlog is a separate bounded recovery path for sessions already idle before startup and for a manually reviewed recovery. `--once` only scans transitions; to inspect already-idle sessions use `--dry-run --backlog-once` with operator approval.

For a failed callback or missed transition, first inspect the Harness synthesis audit/candidate for the exact session and correlation. If outcome is known not accepted, use an explicitly approved bounded recovery; if POST acceptance is ambiguous (timeout/reset), **do not replay automatically**—read the audit/candidate state first. Preserve the production digest ledger; it records accepted transcript digests and must not be erased to force a retry.

Only after that read-back, an operator may explicitly authorize a one-shot recovery (example limit one):

```bash
cd "$REPO"
env HERMES_HOME="$HERMES_HOME" \
  BDH_API_URL="$BDH_API_URL" \
  BDH_SYNTHESIS_POLICY_FILE="$REPO/synthesis-policy.local.json" \
  BDH_SESSION_SYNTH_ENABLED=true \
  BDH_SESSION_SYNTHESIS_LEDGER_FILE="$HERMES_HOME/bdh-session-synthesis-ledger.json" \
  PYTHONPATH="$REPO" PYTHONUNBUFFERED=1 \
  "$PYTHON" session_synthesis_watcher.py --backlog-once --backlog-limit 1
```

This is a **write** operation; it may submit a synthesis request and create a candidate. Do not use it to probe configuration or to replay an ambiguous POST.

The standalone watcher ledger deduplicates only its own producer. It does not share deduplication state with the bridge's in-process idle/finalize buffer/epoch guards. If both producers can see the same session scope, they may stage duplicate syntheses. Prefer one producer per scope; otherwise monitor candidate correlation and review duplicates—do not claim cross-producer deduplication.

## 10. Stop, disable, rollback

Disable only the watcher service on the current OS:

```bash
# macOS
launchctl bootout "gui/$(id -u)/ai.bdh.session-synthesis-watcher"

# Linux
systemctl --user disable --now bdh-session-synthesis-watcher.service
```

For permanent disablement, boot out/disable the service, remove its local plist or user unit, and set `allow_default_core_sessions` false / remove the actor mapping in the local policy. Do not delete the SessionDB, vault, audit files, staging candidates or ledgers as part of disablement. Preserve them unless separately asked to remove data.

For rollback after an unexpected candidate or write: stop the watcher first; preserve logs, policy, ledger, Harness audit and candidate JSON; identify whether any Markdown changed; back up the exact affected files before a reviewed revert. Do not replay a possibly accepted synthesis POST after a timeout: it is non-idempotent.

## 11. Troubleshooting

| Symptom | Likely check |
|---|---|
| Agent is running but no synthesis occurs | Policy absent/invalid, session source not `tui`/`mission-control`, no observed live→idle transition, below `BDH_SESSION_SYNTH_MIN_TURNS`, unresolved actor/vault, or session already recorded in ledger. Check `--check` before inspecting history. |
| Preflight reports database_missing | Verify the selected `HERMES_HOME`, `--db-path`, profile DB locations and OS-user read permissions. Do not proceed as if zero sessions. |
| Preflight reports invalid_or_unreadable | Inspect DB integrity/schema, file permissions, and SQLite/WAL health; do not print or copy transcripts. |
| Preflight reports policy_missing / policy_invalid / no_authorized_routes | Verify `BDH_SYNTHESIS_POLICY_FILE`, valid JSON and an explicit route; never substitute default Harness routing. |
| Preflight reports authorization_unresolved / vault_mismatch | Fix the explicit actor handle→vault mapping or select literal `core` only for the documented default-profile case. Stop if the resolved ID differs from the operator-approved ID. |
| Default profile session is skipped | Expected unless the verified default DB + profile/source gate is satisfied and `allow_default_core_sessions: true` is explicitly configured. |
| A client/project 1:1 session is skipped | Add an explicit addressed `@handle` → exact vault mapping; do not authorize by topic or assume profile identity is enough. |
| Wrong vault appears in dry-run | Stop. Correct the policy/handle/vault ID; do not activate the service. |
| Process exits/restarts repeatedly | On macOS, check `launchctl print` and plist lint; on Linux, check `systemctl --user status` and `systemd-analyze --user verify`. Check executable/imports, HOME/PYTHONPATH, file permissions and stderr/journal logs. |
| HTTP/API failure | Check the exact `BDH_API_URL`, Harness `/health`, selected vault, and provider availability. Do not replay ambiguous POSTs. |
| Candidate not visible | Confirm request reached Harness audit, staging is enabled on the target vault, and use the corresponding `synthesis_id`/session correlation. A ledger row alone is insufficient. |
| Dry-run appears to submit | Confirm `--dry-run` is present and use the session watcher (not the room watcher); session dry-run returns before POST and ledger recording. |
| Old sessions not seen | Live→idle trigger cannot observe an already-idle session. Run a reviewed bounded `--dry-run --backlog-once`, then an explicitly approved bounded recovery if needed. |

## 12. Data handling and limits

The watcher opens Hermes SessionDB read-only, but it reconstructs raw user/assistant text in memory and submits a bounded transcript to the configured Harness endpoint. The selected Harness completion provider may be local or cloud; embeddings have their own provider path. Transcript hashes and audit metadata do not make the transcript anonymous or remove it from Hermes databases, provider requests, candidate staging, or approved notes.

The session watcher has its own digest ledger (`BDH_SESSION_SYNTHESIS_LEDGER_FILE`, default `$HERMES_HOME/bdh-session-synthesis-ledger.json`) and idle state file (`$HERMES_HOME/bdh-session-synthesis-watcher.json`). The room watcher has different ledgers. Do not share ledger files between processes: each process caches and rewrites its ledger. Back up policy, state, ledger, SessionDB and relevant Harness audit/candidate/vault data before destructive maintenance.

## References

- [Deployment templates and room watcher](../deploy/README.md)
- [Session synthesis architecture and limits](session-synthesis.md)
- [Privacy, dry-run and operations](operations.md)
- [Isolated onboarding](quickstart.md)
- [Actor authorization implementation](../synthesis_scope.py)
- [Standalone watcher implementation](../session_synthesis_watcher.py)
