# Installation and operations

## Fixed contract

- Upstream: `https://mcp.linear.app/mcp`.
- Transport: native Codex Streamable HTTP; no local proxy or command.
- Authentication: native OAuth for exactly two final, explicit aliases.
- Credential storage: top-level `mcp_oauth_credentials_store = "keyring"` only. This is a global Codex setting for every MCP OAuth connection, not a Linear-only setting.
- Per-alias policy: `enabled = true`, `default_tools_approval_mode = "writes"`, and `startup_timeout_sec = 60.0`.
- Tool behavior: the official Linear catalog, annotations, calls, and results remain upstream-owned.
- Ownership: one exact marked alias block, plus the keyring segment only when the installer added it.

The generated entries have this shape and do not include `required`, a command, arguments, environment variables, headers, tokens, or a local tool definition:

```toml
mcp_oauth_credentials_store = "keyring"

[mcp_servers.linear-example-a]
url = "https://mcp.linear.app/mcp"
auth = "oauth"
enabled = true
default_tools_approval_mode = "writes"
startup_timeout_sec = 60.0
```

The second explicit alias has the same fields. Examples are sanitized placeholders, not consumer routing defaults.

## Clean install: plan, then apply

Choose both final aliases before any OAuth grant. From the public Ars Operandi repository, plan without writes:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py install \
  --alias linear-example-a \
  --alias linear-example-b
```

The JSON plan reports two one-way alias fingerprints and action names. It does not echo aliases, edit config, install files, run a process, contact a provider, or inspect credentials.

After explicit approval, apply the reviewed plan:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py install \
  --alias linear-example-a \
  --alias linear-example-b \
  --apply
```

A clean apply installs only the credential-free configuration skill and private installer marker. It preserves unrelated config bytes and the existing file mode. An existing top-level `keyring` value is accepted and remains consumer-owned. Missing keyring state is added as an exact managed segment. `auto`, `file`, any other value, duplicate or invalid aliases, existing alias tables, and managed drift are refused rather than rewritten.

## Exact v2 migration

Use migration only when the canonical skill destination contains an unchanged installer-managed v2 marker, tree, and config segment. Plan first with the two final native aliases:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py migrate \
  --alias linear-example-a \
  --alias linear-example-b
```

After approval, add `--apply`. Migration atomically replaces the exact v2 config segment with the native OAuth block and records private staged state containing native config metadata and the two native aliases. For the legacy rollback authority it stores only digests: never legacy references, account selectors, or tokens. The old v2 skill directory, marker, and runtime remain unchanged and available for pre-smoke restore.

No OAuth login, Linear request, process action, or Codex reload occurs inside migration.

## Human activation and evidence gates

For a staged v2 migration, keep the currently running Codex app open on its already-loaded v2 registry throughout pre-restart qualification:

1. Confirm the two alias fingerprints against the final local alias values. Do not rename an alias after OAuth; Codex keys the stored grant by final alias and URL.
2. As visible human gates, run `codex mcp login <final-alias>` once for the first alias and once for the second. Confirm the intended Linear workspace in each browser authorization. The installer never runs these commands.
3. Before quitting or restarting the running app, open a fresh CLI/Codex context and explicitly confirm that it reads the new on-disk native config. Through each alias independently, verify the complete official tool catalog and required annotations, current user/workspace identity, teams, and assigned issues. Compare them with consumer-owned expected identity. A successful login or tool listing alone is not workspace proof.
4. In that same fresh context, verify the external project router selects only the expected managed alias and fails closed for unknown, unavailable, ambiguous, or mismatched identity. Never fall back across workspaces. Explicitly reject the generic plugin-provided Linear connector and every unmanaged Linear connector as fallback routes.
5. If any catalog, identity, team, assigned-issue, or router check fails, run dry-run-first `restore --apply`. Do not restart the app: it still has the v2 registry loaded, so the failed native config never becomes the app's active registry.
6. Only after every pre-restart check passes, quit the Codex app once. Inspect for surviving legacy bridge processes. With separate explicit approval, terminate only the exact legacy bridge launcher and worker command lines that remain; never use a broad name match or terminate unrelated processes.
7. Reopen Codex once and perform a minimal fresh-app discovery plus identity confirmation for both aliases. Then run dry-run-first `finalize`. Finalization does not change config, so it needs no second restart.
8. Preserve only sanitized evidence: alias fingerprint, catalog count/digest, required annotation verdicts, non-sensitive identity/team verdicts, and the minimum assigned-issue identifiers or counts required by the gate. Do not retain browser callbacks, tokens, headers, full private issue content, or provider stderr.

For a clean install, use the same fresh-context qualification before the one app restart when an older running registry is present. There is no v2 runtime to retain or finalize.

Linear writes, credential revocation, and consumer Project Index changes are separate operations and require their own explicit approval.

## Pre-smoke exact restore

If either alias or router check fails during a staged v2 migration, plan restore:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py restore
```

After approval, add `--apply`. Restore validates the unchanged legacy directory/marker against the staged digests, removes the native block and any installer-owned keyring segment, restores the exact v2 config segment, preserves unrelated bytes and mode, and removes only the staged native state. The old runtime was never moved or rewritten.

`rollback` is accepted as an equivalent command for operator continuity. During the staged pre-restart gate, no app reload is needed after restore because the still-running app continues to use its already-loaded v2 registry. A fresh CLI context started after restore reads the restored on-disk config.

## Finalize only after both aliases pass

Once both aliases and the external router pass every read-only gate, plan retirement:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py finalize
```

After approval, add `--apply` only after the one app restart and minimal fresh-app discovery/identity confirmation. Finalize proceeds only while the native config, keyring invariant, staged state, legacy marker, and legacy skill tree are exact. It atomically replaces the legacy v2 directory with the credential-free native configuration skill and removes the staged rollback state. The native config does not change, so finalization does not require a second registry reload. No legacy credential transport runtime remains installed.

After finalization, `restore` removes the exact native block, the installer-owned keyring segment if present, and the managed config skill. It does not recreate the retired v2 runtime. A pre-existing keyring setting is never removed.

## Failure and drift behavior

- Dry runs never create parent directories or write files.
- Config writes use an atomic replacement and retain the prior mode.
- A config failure restores the previous skill/state and config when necessary.
- Alias-set changes, unmanaged alias conflicts, keyring conflicts, marker changes, managed config changes, staged-state changes, and legacy-tree changes fail closed.
- JSON success and error output excludes raw aliases, config contents, legacy references, accounts, exception details, and provider output.
- The installer contains no network, browser, OAuth-login, subprocess, process-termination, or restart implementation.
