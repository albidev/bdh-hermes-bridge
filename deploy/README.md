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

The policy file's default path is anchored to the module directory, so a
daemon whose working directory differs still finds it. Prefer an explicit
`BDH_SYNTHESIS_POLICY_FILE` anyway.

## Verifying a running watcher

The idle watcher fires only on a **live → idle** transition, and
`SessionIdleWatcher` clamps the threshold to a 30-second floor. To see it work,
wait at least ~35s on a room that is genuinely older than the threshold, and
run the script with `-u` / `PYTHONUNBUFFERED=1`: the daemon's stdout is
block-buffered when it is not a terminal, so a silent log file does not mean the
watcher is idle.

```bash
PYTHONUNBUFFERED=1 python room_synthesis_watcher.py --dry-run --once   # no writes
```

`--dry-run` prints the exact payload it would post, including the resolved
`vault_id`, without contacting BDH.
