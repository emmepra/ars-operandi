# Installation and operations

## Fixed contract

- Upstream: `https://mcp.linear.app/mcp`.
- Local transport: one STDIO process per workspace.
- 1Password account: explicit local installer input; sanitized example `example.1password.com`.
- Connection: repeatable explicit `ALIAS=OP_REFERENCE` input; sanitized examples `linear-example-a=op://Example/linear-a/credential` and `linear-example-b=op://Example/linear-b/credential`.
- Default tool approval mode: `writes` for every generated alias.
- Startup timeout: 60 seconds for every generated alias.

The bridge syntactically validates and resolves its single static reference once with `op inject`, removes inherited 1Password account/session/service-account/Connect authentication variables from that subprocess, and retains the resolved Bearer only in process memory. It neither persists nor prints the value. There is no alternate endpoint, runtime credential switch, provider fallback, or locally defined Linear tool.

## Plan first

From the public Ars Operandi repository:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py install \
  --op-account example.1password.com \
  --connection linear-example-a=op://Example/linear-a/credential \
  --connection linear-example-b=op://Example/linear-b/credential
```

The dry run reports only aliases and planned actions. It does not install the skill or edit Codex config.

After explicit approval, apply the already-reviewed plan:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py install \
  --op-account example.1password.com \
  --connection linear-example-a=op://Example/linear-a/credential \
  --connection linear-example-b=op://Example/linear-b/credential \
  --apply
```

The installer copies the public skill to `~/.agents/skills/official-linear-mcp-bridge`, runs the exactly pinned script once to prepare dependencies, verifies `uv run --offline --script ... --help`, and only then adds one marked block to `~/.codex/config.toml`. These checks receive no reference, account, credential, or Linear request. Each alias explicitly sets `default_tools_approval_mode = "writes"` and `startup_timeout_sec = 60.0`; official annotations remain authoritative while write tools require approval and startup is bounded. The installer does not call `codex mcp add/remove`, normalize unrelated tables, or place resolved credentials in config.

## Resume an exact emergency pause

If an operator paused every installer-managed connection by changing all and only
its generated `enabled = true` lines to `enabled = false`, plan the exact resume:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py install \
  --op-account example.1password.com \
  --connection linear-example-a=op://Example/linear-a/credential \
  --connection linear-example-b=op://Example/linear-b/credential \
  --resume-paused
```

The flag is install-only and still defaults to a no-write dry run. It requires the
same account, aliases, references, valid skill marker, and exact marked segment as
the prior install. Without the flag, paused state remains managed drift. With it,
any byte change beyond every generated enable line changing to `false` is refused.
The plan reports only a sanitized `resume-exact-paused-connection-block` action.

After reviewing that plan, add `--apply`. The installer atomically stages the new
skill, runs credential-free preparation and offline verification, and only then
restores the exact managed enable lines to `true`. Runtime or config failure
restores the previous skill and leaves the paused config unchanged. This command
does not invoke `op`, contact Linear, or restart Codex.

## Rollback

Plan exact rollback first:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py rollback
```

After approval:

```bash
python3 skills/official-linear-mcp-bridge/scripts/install_official_linear_mcp_bridge.py rollback --apply
```

Rollback requires an unchanged managed skill and exact managed TOML segment, including any separator added during installation. It removes only those targets, restores a pre-existing missing final newline byte-for-byte, and preserves unrelated changes made before or after the segment.

## Human and evidence gates

1. Replace every sanitized example with explicit local values. The public skill contains no consumer aliases, references, or account selector.
2. Unlock the 1Password desktop app when the STDIO process starts. Authentication prompts are visible human gates.
3. Restart Codex after config apply or rollback; the installer never restarts it.
4. For each alias, preserve only: catalog count/digest, required annotation checks, non-sensitive workspace/team identity, and assigned-issue count/identifiers needed for the gate.
5. Do not preserve credentials, HTTP headers, full issue bodies, private tool results, browser callbacks, or provider stderr.
6. Keep the prior adapter configured as rollback until every alias independently passes catalog, identity, and assigned-issue read-only smoke checks and the external fail-closed router selects the expected alias.
