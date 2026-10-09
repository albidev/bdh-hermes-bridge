# Bridge quickstart: isolated discovery and stats

The bridge is optional. First complete the Harness [standalone demo](https://github.com/albidev/bdh-graph-harness/blob/main/docs/quickstart.md) and leave that disposable server running at `http://127.0.0.1:18643`.

**Do not enable this plugin in a production chat merely to test it.** `pre_llm_call` can retrieve context automatically, and `post_api_request` can submit completed eligible turns for learning. Disabling rewrite or session synthesis is **not** a universal automatic-write disable. The initial check below invokes no conversation hooks and dispatches only `bdh_stats`; `bdh_query` is not a read-only verification tool.

## Prerequisite

Use the Python interpreter belonging to your existing Hermes installation, with `hermes_cli.plugins` and `tools.registry` importable. Set `HERMES_PYTHON` to that interpreter's absolute path. Do not install the bridge into Hermes core or modify core source. For a fresh test environment instead, see [compatibility](compatibility.md).

The check is local, uses a separate `HOME` and `HERMES_HOME`, and does not clone your production home, credentials, DBs or plugin configuration. POSIX shell example:

```bash
export BDH_BRIDGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/bdh-bridge-demo.XXXXXX")"
mkdir -p "$BDH_BRIDGE_ROOT/home/.hermes/plugins"
git clone https://github.com/albidev/bdh-hermes-bridge.git \
  "$BDH_BRIDGE_ROOT/home/.hermes/plugins/bdh-hermes-bridge"
# Set this to the Python interpreter used by your Hermes installation:
export HERMES_PYTHON="/absolute/path/to/hermes/python"
"$HERMES_PYTHON" -c 'from hermes_cli.plugins import PluginManager; from tools.registry import registry; print("Hermes imports OK")'
```

Write only the **new isolated** config:

```bash
python3 -c 'import os; from pathlib import Path; p=Path(os.environ["BDH_BRIDGE_ROOT"])/"home/.hermes/config.yaml"; p.write_text("plugins:\n  enabled:\n    - bdh-hermes-bridge\n")'
```

## Exercise the real Hermes registry, not a mock registrar

```bash
env -i PATH="$PATH" \
  HOME="$BDH_BRIDGE_ROOT/home" \
  HERMES_HOME="$BDH_BRIDGE_ROOT/home/.hermes" \
  BDH_API_URL=http://127.0.0.1:18643 \
  BDH_QUERY_REWRITE_ENABLED=false BDH_SESSION_SYNTH_ENABLED=false \
  "$HERMES_PYTHON" -I -c '
import json
from hermes_cli.plugins import PluginManager
from tools.registry import registry
manager = PluginManager()
manager.discover_and_load()
result = registry.dispatch("bdh_stats", {})
if isinstance(result, str):
    result = json.loads(result)
assert not result.get("error"), result
assert result.get("neuron_count", 0) > 0, result
print(json.dumps(result, indent=2))
'
```

Success means the plugin was discovered/registered and the real `bdh_stats` handler read a populated demo graph. An error or all-zero graph is **not** success. This does not verify a live conversation, routing for your private clients, synthesis, or curation. No Mission Control registry or production gateway restart is needed for this isolated check.

If imports fail, use the correct Hermes interpreter rather than adding arbitrary core paths to the system Python. If no tool is registered, check the isolated config, manifest and plugin loading errors. If stats fail, check the demo server/config/port. Run the Harness smoke command before and after this check to verify the demo learning artifacts stay unchanged.

## Deliberate rollout later

1. Back up the target profile configuration and data. Choose one disposable/test profile and one explicit vault before enabling hooks.
2. Install/enable `bdh-hermes-bridge` in that profile using its `plugins.enabled` list. The manifest registers six hooks and two tools; the optional idle watcher is bridge-owned, not a Hermes `on_session_idle` registration.
3. Set `BDH_API_URL` for the **owning process** and review deterministic vault mapping. Feature flags are read at import/startup; changing a shell export does not update a running gateway.
4. Review [operations/privacy](operations.md) and [synthesis](session-synthesis.md) before any real turns. A local deployment is an operator choice, not a hard-coded privacy guarantee. Explicitly select `llm.local_only` / source overrides in Harness if required.
5. The standalone room/session watchers are an optional advanced deployment, not a prerequisite for basic tools. For the complete agent-oriented session watcher procedure (actor policy, dry-run, safe backlog settings, LaunchAgent, verification and rollback), use [session watcher setup](session-watcher-setup.md); the low-level [watcher deployment templates](../deploy/README.md) are for operators rendering the agents.
6. New plugin code/config loads in a new owning process. Restart only that test profile's process after approval—never another user's gateway by habit.

Remove the isolated plugin/config or retain the disposable root for debugging. Nothing here authorizes deleting your real Hermes home or BDH vault.

Next: [compatibility](compatibility.md), [operations](operations.md), [semantic retrieval router](semantic-vault-router.md), [watcher deployment](../deploy/README.md).
