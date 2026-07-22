---
name: official-linear-mcp-bridge
description: Install or operate the narrow Ars Operandi STDIO bridge that binds one explicitly supplied 1Password Linear credential to the official Linear Streamable HTTP MCP while preserving the upstream tool catalog, annotations, calls, and results. Use for temporary official-Linear connections, config planning, runtime preparation, drift checks, or exact rollback. Do not use it to implement Linear tools, routing, identity policy, or domain behavior.
---

# Official Linear MCP bridge

Use this skill only for temporary transport connections. Each process binds exactly one syntactically validated `op://` reference and explicit 1Password account to the pinned official Linear endpoint, then exposes the official upstream catalog unchanged over STDIO.

1. Keep project-to-alias routing outside this bridge and fail closed before selecting a server when the project is unknown or ambiguous.
2. Supply the local account and every `ALIAS=OP_REFERENCE` connection explicitly. Run the installer without `--apply` first and inspect only the planned aliases and actions; references are never printed.
3. Apply only after explicit user approval. The installer edits only its marked TOML block, preserves other config bytes and file mode, and refuses managed drift.
4. Apply prepares the exactly pinned MCP runtime without credentials, verifies the configured offline command, and only then writes config. Restart Codex only after an approved apply, then use read-only identity, catalog, and assigned-issue smoke checks for each alias.
5. Do not attempt a live write through this skill without separate, explicit approval. Official tool annotations must remain unchanged so Codex can gate writes.
6. Retain any existing rollback adapter until every official alias passes the required smoke gates.

See [installation and operations](references/installation-and-operations.md) for dry-run, apply, rollback, and evidence boundaries.
