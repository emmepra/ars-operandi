# Installation and operations

## Prerequisites

- `codex`, `uv`, and 1Password CLI (`op`) on the same Mac.
- 1Password desktop integration enabled for the selected account.
- An explicit consumer manifest path.
- One 1Password item per manifest `credential_profile`, under one deterministic non-secret template such as `op://Example/{profile}/credential`.

Use a concealed `credential` field and item titles that match the manifest profiles. The manifest, installer arguments, and Codex config contain only paths, aliases, an account selector, and the `op://` template—never a Linear token or 1Password session.

## 1Password desktop integration

For Codex-launched live processes, select `--op-auth-mode direct` and an explicit `--op-account`. With 1Password desktop app integration enabled, one `op inject --account <selector>` resolves all required profiles from an in-memory stdin template. The adapter does not call `op signin`, create a shell session, write a resolved template to disk, or place an `OP_SESSION` in Codex configuration.

The inject child starts from an environment with inherited `OP_SESSION*`, `OP_ACCOUNT`, `OP_SERVICE_ACCOUNT_TOKEN`, `OP_CONNECT_TOKEN`, and `OP_CONNECT_HOST` removed. The parent environment is never mutated. Timeout, malformed bundle, or provider failure is terminal for that process: there is no retry, signin, ambient-session fallback, or credential switching.

`--op-auth-mode ephemeral` remains an explicit compatibility alternative for environments that deliberately use a short-lived `op signin --raw` session. It is not installed by default. Do not configure either mode with a session or provider token in Codex.

## Validate before installation

These checks are credential-free:

```bash
uv run --script /absolute/path/to/dual_linear_mcp.py validate-manifest \
  --manifest /absolute/path/to/projects.yaml

uv run --script /absolute/path/to/dual_linear_mcp.py resolve \
  --manifest /absolute/path/to/projects.yaml \
  --connection-alias linear-alpha
```

## Idempotent skill and MCP installation

The installer copies the skill to `~/.agents/skills/dual-linear-mcp` and registers one read-only-by-default STDIO MCP alias: `dual-linear`. It does not symlink a worktree. Its default is dry-run; `--apply` is required for changes.

During apply, the installer runs a credential-free manifest validation once to prepare the `uv` environment. The registered MCP uses `uv run --offline`, so dependency resolution cannot consume Codex's startup window. Installer calls to `codex mcp` also set a non-secret wrapper-bypass flag and remove inherited 1Password auth, preventing an unrelated shell wrapper from opening a second signin.

Run the exact command once without `--apply`, review the JSON actions, then repeat it with `--apply`:

```bash
python3 /absolute/path/to/ars-operandi/skills/dual-linear-mcp/scripts/install_dual_linear.py install \
  --manifest /absolute/path/to/projects.yaml \
  --op-reference-template 'op://Example/{profile}/credential' \
  --op-account example

python3 /absolute/path/to/ars-operandi/skills/dual-linear-mcp/scripts/install_dual_linear.py install \
  --manifest /absolute/path/to/projects.yaml \
  --op-reference-template 'op://Example/{profile}/credential' \
  --op-account example \
  --apply
```

The installed MCP command is exactly the following non-secret shape:

```text
uv run --offline --script ~/.agents/skills/dual-linear-mcp/scripts/dual_linear_mcp.py serve
  --manifest /absolute/path/to/projects.yaml
  --op-reference-template op://Example/{profile}/credential
  --op-auth-mode direct
  --op-account example
  --auth-scheme api-key
```

The installer records a content hash and managed marker inside the copied skill. Repeating the same desired state is a no-op. A locally modified managed copy, an unmanaged destination, or a `dual-linear` MCP config that differs from the recorded state fails closed; nothing is silently overwritten. The installer never prints another MCP configuration.

The server starts one credential preload in a background thread without waiting for it before serving the STDIO transport. Direct desktop integration performs no signin and resolves every distinct verified profile through one `op inject` process into the memory cache. After preload succeeds, repeated MCP tool calls make no additional 1Password calls and cannot create prompt loops.

Live tools return `secret_preload_pending` until preload finishes. A locked or unavailable desktop app, desktop authorization failure, or profile read failure is cached for the process and fails closed with one sanitized instruction to unlock 1Password and restart the MCP server. This keeps interactive authentication off the blocking startup path without persisting a session or deferring identity checks.

## Post-install config and runtime smoke

First run the credential-free managed-install smoke. Its verdict is only `config-verified`: it checks the managed hash, exact MCP registration, and prepared offline runtime, not a Linear workspace.

```bash
python3 ~/.agents/skills/dual-linear-mcp/scripts/install_dual_linear.py smoke
codex mcp get dual-linear --json
```

Then restart the MCP from Codex desktop under **Settings > MCP servers > Restart** and inspect `/mcp`. If the skill is absent or stale, restart or refresh the Codex task/app so skill discovery reruns.

For two `planned` connections, run one read-only identity bootstrap with both explicit public-style aliases:

```bash
uv run --script ~/.agents/skills/dual-linear-mcp/scripts/dual_linear_mcp.py bootstrap \
  --manifest /absolute/path/to/projects.yaml \
  --connection-alias linear-alpha \
  --connection-alias linear-beta \
  --op-reference-template 'op://Example/{profile}/credential' \
  --op-auth-mode direct \
  --op-account example
```

Compare each returned stable workspace/team identity with an independent trusted account view. Bootstrap never edits the manifest. After the consumer owner records both stable IDs and changes both bindings to `verified`, run the exact MCP handshake/list-tools/two-workspace smoke:

```bash
uv run --script ~/.agents/skills/dual-linear-mcp/scripts/smoke_dual_linear_mcp.py \
  --connection-alias linear-alpha \
  --connection-alias linear-beta
```

This command launches the exact registered read-only STDIO command, waits only while the one process-local credential preload is pending, lists the MCP tools, and invokes `linear_discover` once per explicit alias. Success is `runtime-verified` with two independently matched workspace IDs. Auth, identity, unknown-alias, or tool-contract failures stop without fallback. Finally restart `dual-linear` in Codex and repeat the two `linear_discover` calls from the refreshed task to prove client-side discovery.

## Mutation gate

The installed config intentionally omits `--enable-mutations`. Do not add it without separate approval for the target workspace and operation. A write still requires `confirm=true`, identity preflight, and read-back. There is no delete/archive cleanup tool in this version.

## Rollback

Rollback is also dry-run-first and removes only state that matches the installer marker and recorded MCP config:

```bash
python3 ~/.agents/skills/dual-linear-mcp/scripts/install_dual_linear.py rollback
python3 ~/.agents/skills/dual-linear-mcp/scripts/install_dual_linear.py rollback --apply
```

After rollback, restart/refresh Codex to terminate the old STDIO process and clear its in-memory cache. Rollback does not change the consumer manifest, 1Password items, or Linear data. If either installed surface has drifted, rollback stops for operator review.

## Upstream references

- [Linear GraphQL getting started](https://linear.app/developers/graphql)
- [1Password CLI secret loading](https://developer.1password.com/docs/cli/secrets-scripts)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp/)
