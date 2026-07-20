---
name: dual-linear-mcp
description: Use when Codex must route Linear reads or explicitly approved issue mutations across multiple workspaces, when a project manifest selects credential profiles, or when account drift, identity mismatches, and post-write verification must fail closed.
---

# Dual Linear MCP

Route every operation from an explicit manifest selector to exactly one verified Linear connection. Never guess, fall back to another credential, or treat a configured alias as proof of identity.

## Operating sequence

1. Require an explicit manifest path. Read [manifest-and-tools.md](references/manifest-and-tools.md) when shaping or debugging bindings.
2. Run `validate-manifest`, then `resolve`; both are credential-free.
3. If a connection is `planned`, run the explicit read-only `bootstrap` command. Treat its stable ID as a candidate for independent operator review; the command never edits or activates the manifest.
4. Use explicit `--op-auth-mode ephemeral --op-account <selector>` for live startup. The process signs in once, strips inherited 1Password auth, and keeps session and Linear tokens only in memory/private child environments.
5. Start the MCP server without `--enable-mutations`. It opens the MCP handshake immediately and preloads every verified profile once in a background thread; live tools fail closed as pending or failed until preload completes.
6. Use `resolve_linear_route`, `linear_discover`, and `linear_get_issue` before considering writes.
7. Enable mutations only after explicit approval. Require both server enablement and `confirm=true` on every write.
8. Treat any workspace, team, project, auth, or read-back mismatch as a hard failure. Do not retry or switch connections.

## Commands

```bash
uv run --script scripts/dual_linear_mcp.py validate-manifest \
  --manifest /absolute/path/to/projects.yaml

uv run --script scripts/dual_linear_mcp.py resolve \
  --manifest /absolute/path/to/projects.yaml \
  --project sample-project

uv run --script scripts/dual_linear_mcp.py bootstrap \
  --manifest /absolute/path/to/projects.yaml \
  --connection-alias linear-alpha \
  --connection-alias linear-beta \
  --op-reference-template 'op://Example/{profile}/credential' \
  --op-auth-mode ephemeral \
  --op-account example
```

For the dry-run-first installer, alias `dual-linear`, post-install discovery, restart, and rollback, read [installation-and-operations.md](references/installation-and-operations.md).

## Safety rules

- Keep secrets in 1Password; never put tokens in the manifest, MCP arguments, examples, logs, or errors.
- Never configure `OP_SESSION`, service-account tokens, Connect tokens, or Linear tokens in Codex. Ephemeral auth accepts only a non-secret account selector and `op://` template.
- Keep `binding_state` at `planned` or `blocked` until a stable workspace ID is known. Only `verified` routes are callable.
- Bootstrap only an explicit non-blocked alias. Review its workspace/team identity independently before a consumer owner records the candidate ID and changes the binding to `verified`.
- Prefer a project selector. Use an explicit connection alias only when project context is genuinely unavailable.
- Run discovery before mutation. A mutation checks workspace and team before the write, then independently reads the issue back and checks team, project binding, and changed fields.
- Do not add a database, implicit manifest search, credential fallback, delete tool, or generic GraphQL tool.

## Verification

Run the adapter suite and repository skill validator from the Ars Operandi root:

```bash
uv run --with 'mcp>=1.27,<2' --with 'pyyaml>=6,<7' \
  python -m unittest discover -s tests -p 'test_*.py'
python3 scripts/validate_skills.py
```

Fake/local success does not verify real Linear permissions, 1Password desktop integration, Codex process restart behavior, or either live workspace.
