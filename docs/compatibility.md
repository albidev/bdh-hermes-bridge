# Compatibility and alpha support

This is experimental software, not a versioned stable API promise. The bridge is standalone: do not patch Hermes core to install it.

## Automated matrix

[CI](../.github/workflows/ci.yml) runs on `ubuntu-latest` with Python **3.11, 3.12 and 3.13**, installing [requirements-ci.txt](../requirements-ci.txt). That file currently selects `hermes-agent>=0.19,<0.20`. This is the **test dependency range**, not proof that every older/newer core version is supported or that every hook first appeared in 0.19.

A local source checkout may differ from the CI-installed package. Report the exact Hermes version/commit and Python when filing a failure. No native Windows/macOS CI or all-provider guarantee is implied by the Linux matrix.

## Actual integration surface

[plugin.yaml](../plugin.yaml) and [register()](../__init__.py) declare six hooks:
`pre_llm_call`, `post_api_request`, `post_tool_call`, `transform_llm_output`, `on_session_finalize`, `on_session_reset`; and two tools: `bdh_query` (required `query`, optional `vault_id`) and `bdh_stats` (optional `vault_id`). The idle watcher is bridge-owned, not a registered Hermes idle hook.

Manifest version and source metadata currently both say **0.12.0**. This is a plugin version, not a release tag or a Hermes core version. No unverified minimum-version manifest field is added here.

## Fresh test environment (not a production Hermes install)

From this checkout, create a disposable environment with Python 3.11:

```bash
python3.11 -m venv .test-venv
. .test-venv/bin/activate
python -m pip install -r requirements-ci.txt
python -c 'import importlib.metadata as m; print(m.version("hermes-agent"))'
```

Use that interpreter for the isolated discovery/stats [quickstart](quickstart.md). Dependencies belong to this disposable environment, not the active Hermes runtime. For the suite command, see the [CI workflow](../.github/workflows/ci.yml).

## Before an alpha release

- Verify install + isolated registry dispatch against a populated Harness demo.
- Test exact hook/scope and tool behavior on your intended Hermes version, not only metadata.
- Verify local-only source routing explicitly if required by policy.
- Review changes to both projects' contracts before upgrading either.
- Record OS/Python/Hermes/dependency versions and distinguish real provider E2E from mocked tests.
- Keep production DBs, credentials, policy/index files and vaults out of published artifacts.
- Do not advertise watcher/curation/network production readiness from a read-only stats check.
