---
name: official-linear-mcp-bridge
description: Install exactly two native Codex Streamable HTTP OAuth connections to the official Linear MCP with operating-system keyring storage, exact config ownership, staged v2 bridge migration, restore, and finalization. Use for dry-run-first native Linear MCP setup or migration. Do not use it to log in, proxy tools, manage credentials, choose project routes, verify workspace identity, or perform Linear writes.
---

# Official Linear MCP native installer

The legacy skill name is retained so the installer can recognize and safely retire its installer-managed v2 bridge. New and finalized installations contain only this credential-free configuration skill; there is no local Linear transport runtime.

1. Choose the two final Codex aliases before OAuth. Project-to-alias routing and expected workspace identity remain consumer-owned and must fail closed.
2. Run `install` for a clean setup or `migrate` for an exact v2 managed install, always without `--apply` first. Plans expose only alias fingerprints and action names.
3. Apply only the reviewed plan. The installer requires the top-level OAuth store to be `keyring`, refuses `auto`, `file`, alias conflicts, and managed drift, and preserves unrelated config bytes and mode. This top-level setting is global to every Codex MCP OAuth connection, not only Linear.
4. The installer never starts a process, contacts Linear, opens a browser, logs in, terminates or restarts Codex, or handles a credential. Keep the running Codex app on its v2 registry while completing both visible OAuth logins and native qualification through a fresh CLI/Codex context that reads the new on-disk config.
5. Before quitting the running app, verify the official catalog, user/workspace identity, teams, assigned issues, and external router independently through each native alias. Unknown, unavailable, or mismatched identity fails closed without cross-workspace fallback. The generic plugin-provided Linear connector and every unmanaged Linear connector are forbidden fallbacks. On any failure, use `restore --apply`; no app restart is needed because the running app still has v2.
6. Only after every pre-restart check passes, quit Codex once. With separate explicit approval, terminate only exact legacy bridge launcher/worker commands that are still present, then reopen Codex and run a minimal fresh-app discovery and identity confirmation. Do not perform a second restart.
7. A staged v2 migration keeps the old skill/runtime untouched as pre-smoke rollback authority. Its staged state stores native config metadata and aliases, but only digests for legacy authority—never legacy references, accounts, or tokens. Use dry-run-first `finalize` after the fresh-app confirmation; finalization replaces the old runtime with this config-only skill without changing config or requiring another restart.
8. Linear writes, credential revocation, and consumer routing changes require separate explicit approval and are outside this skill.

See [installation and operations](references/installation-and-operations.md) for commands, state transitions, human gates, evidence boundaries, and restore behavior.
