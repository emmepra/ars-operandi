# Installation and operations

## Prerequisites

- `uv` for the script-scoped Python environment.
- 1Password CLI authenticated in the same host/session context that starts the MCP process.
- One 1Password secret item per manifest `credential_profile`.
- A non-secret `op://` template containing `{profile}`, for example `op://Operations/{profile}/api-key`.

The server uses `op read` without a shell. It preloads every verified profile at process start and caches secret values only in memory. Startup fails if any verified profile cannot be loaded. Restart the MCP server after credential rotation or re-authentication.

Linear personal API keys use the default `--auth-scheme api-key`. For OAuth access tokens, start with `--auth-scheme bearer`.

## Validate without installing

The safe dry run is the credential-free manifest and route check:

```bash
uv run --script /absolute/path/to/dual_linear_mcp.py validate-manifest \
  --manifest /absolute/path/to/projects.yaml

uv run --script /absolute/path/to/dual_linear_mcp.py resolve \
  --manifest /absolute/path/to/projects.yaml \
  --connection-alias linear-alpha
```

There is intentionally no mutation dry-run that pretends a write occurred. The MCP server is read-only by default because mutation handlers reject calls unless `--enable-mutations` is present.

## Add a read-only Codex STDIO server

Review the command before running it; this changes Codex MCP configuration but does not install Python packages globally:

```bash
codex mcp add dual-linear -- \
  uv run --script /absolute/path/to/dual_linear_mcp.py serve \
  --manifest /absolute/path/to/projects.yaml \
  --op-reference-template 'op://Operations/{profile}/api-key'
```

For project-scoped configuration in a trusted repository, use this generic `.codex/config.toml` shape:

```toml
[mcp_servers.dual_linear]
command = "uv"
args = [
  "run",
  "--script",
  "/absolute/path/to/dual_linear_mcp.py",
  "serve",
  "--manifest",
  "/absolute/path/to/projects.yaml",
  "--op-reference-template",
  "op://Operations/{profile}/api-key",
]
required = true
enabled_tools = [
  "resolve_linear_route",
  "linear_discover",
  "linear_get_issue",
]
default_tools_approval_mode = "writes"
```

Restart the Codex client after configuration, then inspect the server with `codex mcp list` or `/mcp`. Use discovery with each connection and compare the returned workspace/team IDs with the intended manifest binding before enabling any mutation.

## Temporarily enable mutations

Do this only under explicit approval for the target workspace and operation:

1. Add `--enable-mutations` to the server arguments.
2. Add only `linear_create_issue` and/or `linear_update_issue` to the client `enabled_tools` list.
3. Restart the server so secrets are freshly loaded.
4. Run `linear_discover` first.
5. Call the mutation with one project or connection selector and `confirm=true`.
6. Require `verification: read-back-verified` before treating the operation as complete.
7. Remove mutation enablement after the approved operation window.

## Rollback and removal

- Disable the config entry with `enabled = false`, or remove it with `codex mcp remove dual-linear`.
- Restart Codex to terminate the process and clear the in-memory secret cache.
- Remove the project-scoped configuration block if one was added.
- Removing the adapter does not change the manifest, 1Password items, or Linear data.
- If a mutation succeeded but read-back failed, inspect that one workspace manually before retrying. Do not retry through another credential.

## Upstream references

- [Linear GraphQL getting started](https://linear.app/developers/graphql)
- [1Password CLI secret loading](https://developer.1password.com/docs/cli/secrets-scripts)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
