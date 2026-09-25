# Watcher deployment

Both watchers are `launchd` agents. The plists in this directory are
**templates**: they use `__PYTHON__`, `__REPO__`, `__HERMES_HOME__` and
`__ROOM_REGISTRY__` placeholders instead of machine paths, so the repository
stays portable. A committed plist with absolute paths goes stale the moment
the checkout moves (a worktree path, a rename, a second machine).

## Rendered agents

| Agent | Script | Purpose |
|---|---|---|
| `ai.bdh.session-synthesis-watcher` | `session_synthesis_watcher.py` | idle TUI / Mission Control sessions |
| `ai.bdh.room-synthesis-watcher` | `room_synthesis_watcher.py` | hosted group rooms |

Install by rendering a template into `~/Library/LaunchAgents/` and bootstrapping
it:

```bash
REPO=/path/to/bdh-hermes-bridge
HERMES_HOME=$HOME/.hermes
ROOM_REGISTRY=$HOME/Projects/hermes-mission-control/server/room_vaults.json
PYTHON=$HERMES_HOME/hermes-agent/venv/bin/python

for agent in ai.bdh.session-synthesis-watcher ai.bdh.room-synthesis-watcher; do
  sed -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__REPO__|$REPO|g" \
      -e "s|__HERMES_HOME__|$HERMES_HOME|g" \
      -e "s|__ROOM_REGISTRY__|$ROOM_REGISTRY|g" \
      "deploy/$agent.plist" > "$HOME/Library/LaunchAgents/$agent.plist"
  launchctl bootout "gui/$(id -u)/$agent" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$agent.plist"
done
```

## Required configuration

Both watchers are **fail-closed**. They authorize a synthesis target from the
actor (serving profile / room members), never from transcript text, and they
skip the flush entirely when nothing is authorized. That requires:

- **`BDH_SYNTHESIS_POLICY_FILE`** — the local policy mapping profile prefixes to
  vaults. Gitignored (`synthesis-policy*.json`); it holds operator routing
  config, not repository content. **Without it both watchers synthesize
  nothing** — that is the intended failure mode, not a bug.
- **`BDH_ROOM_VAULT_REGISTRY`** — room → vault registry, written by Mission
  Control. Only the room watcher needs it. Note the registry is per-*room*, so a
  room mapped to a client vault is only a starting point: the member profiles
  must agree, and a room with a `default` participant is refused.
- **`BDH_SYNTHESIS_LEDGER_FILE`** — room watcher only. Records the transcript
  digest last submitted per room, so re-synthesis is decided by CONTENT rather
  than by an observed transition. Defaults to
  `$HERMES_HOME/bdh-synthesis-ledger.json`. Delete it to force one full
  re-synthesis pass.

The policy file's default path is anchored to the module directory, so a
daemon whose working directory differs still finds it. Prefer an explicit
`BDH_SYNTHESIS_POLICY_FILE` anyway.

### Core sessions (explicit opt-in)

The default Hermes profile is not a client actor. To let its idle TUI/Mission
Control sessions stage candidates in the **Core** vault, set
`"allow_default_core_sessions": true` in the local gitignored policy file.
This applies only when the session is found in the default `state.db`, has
`profile_name=default`, and has source `tui` or `mission-control`. An addressed
`@handle` takes precedence; unknown/ambiguous handles, secondary profile
DBs, rooms, and cron sessions never fall back to Core. The flag defaults to
false; it does not enable an implicit target for other profiles.

Back up the policy before enabling it, then inspect
`session_synthesis_watcher.py --dry-run --backlog-once --backlog-limit 3`
before restarting the watcher. Startup recovery can saturate the BDH API
until its bounded batch completes; Curate may time out temporarily. Verify
both the Core synthesis audit and `pending_review` candidates afterward —
the ledger alone only proves BDH accepted the request.

## Why the room watcher has a recovery pass

The idle trigger is a **live → idle transition**, and that is correct for
steady state: without it a quiet room would be re-synthesized on every poll.

It is not sufficient on its own. A room that was already quiescent *before the
daemon started* never crosses that transition while being observed — the state
it must leave is the state it is already in — so it can never be synthesized,
and nothing in the logs distinguishes that from "nothing to synthesize".

The room watcher therefore runs one **bounded backlog pass at startup**
(`--backlog-limit`, default 3; `0` disables). Eligibility is content-based via
the ledger, so the pass submits a room once and a restart does not re-submit it.
New messages change the digest and reopen the room.

```bash
# Inspect the backlog without posting anything.
PYTHONUNBUFFERED=1 python room_synthesis_watcher.py --backlog-once --dry-run
```

## The session watcher has the same pass, and why

The **session** watcher had the same blind spot, one layer down: a session whose
`live → idle` crossing happened without anyone acting on it was unreachable
forever. Two ways that happens — it went idle while ineligible (too few turns, or
no authorised vault, so the crossing was consumed and it stays `idle`), or it
went idle while this process was not running (so it has no recorded state at
all). Neither is fixed by waiting, because neither will produce another crossing.

Measured on the live instance before the fix: **3 sessions** in that state, one of
them 12 turns of client work — against 11 sessions the actor gate correctly
refuses. So the backlog pass is bounded (`--backlog-limit`, default 3) and
newest-first, and it applies the actor gate and the minimum-turn floor before the
ledger, so recovery can never submit something the live path would refuse.

```bash
# Inspect the session backlog without posting anything.
PYTHONUNBUFFERED=1 python session_synthesis_watcher.py --backlog-once --dry-run
```

**The two watchers use SEPARATE ledger files.** `SynthesisLedger` caches its
contents at first load and rewrites the whole file on every record, so two
processes sharing one file would overwrite each other's entries:

| watcher | env var | default |
|---|---|---|
| room | `BDH_SYNTHESIS_LEDGER_FILE` | `$HERMES_HOME/bdh-synthesis-ledger.json` |
| session | `BDH_SESSION_SYNTHESIS_LEDGER_FILE` | `$HERMES_HOME/bdh-session-synthesis-ledger.json` |

## Verifying a running watcher

The idle watcher fires only on a **live → idle** transition, and
`SessionIdleWatcher` clamps the threshold to a 30-second floor. To see it work,
wait at least ~35s on a room that is genuinely older than the threshold, and
run the script with `-u` / `PYTHONUNBUFFERED=1`: the daemon's stdout is
block-buffered when it is not a terminal, so a silent log file does not mean the
watcher is idle. Note that `--once` alone emits nothing for an already-idle
room by design — use `--backlog-once` to reach that case.

```bash
PYTHONUNBUFFERED=1 python room_synthesis_watcher.py --dry-run --once   # no writes
```

`--dry-run` prints the exact payload it would post, including the resolved
`vault_id`, without contacting BDH. In dry-run mode the ledger IS written (the
pass is simulated, not skipped), so a dry-run then suppresses a real pass for
the same content — clear the ledger file if you want to repeat it.
