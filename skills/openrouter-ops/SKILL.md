---
name: openrouter-ops
description: Use when Codex needs to operate OpenRouter safely: inspect management-key access, list workspaces or keys, create workspace-scoped ephemeral runtime API keys with spending limits and expirations, run project commands with OPENROUTER_API_KEY, and delete/disable/rotate OpenRouter keys without exposing secrets.
---

# openrouter-ops

Use this skill for OpenRouter provider operations from Codex. Keep OpenRouter general: Ariadne, Inspect, or any other project may consume the runtime key, but this skill owns the provider-operation boundary.

## Security Rules

- Do not ask the user to paste OpenRouter keys in chat.
- Do not print, log, commit, or save plaintext keys.
- Treat Management API keys and runtime API keys as different credentials.
- Bootstrap Management API keys manually in the OpenRouter UI, then store them in 1Password.
- Use 1Password only as the trust store for the Management API key.
- Do not store runtime keys in 1Password by default. Create runtime keys for one command, inject them into the subprocess environment, then delete them.
- Use `--live` only when the user explicitly wants a real OpenRouter mutation.
- Use workspace IDs or slugs for key creation. Slugs are resolved live through the Management API.

## CLI

The deterministic helper is:

```bash
python3 skills/openrouter-ops/scripts/openrouter_ops.py --help
```

The CLI never accepts plaintext secrets as command arguments. Management operations expect the management key in an environment variable already resolved by `op run`:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py preflight --check-api
```

For runtime use, prefer `run-ephemeral`: the helper creates a temporary runtime key, runs the target command with `OPENROUTER_API_KEY`, captures/redacts output, and deletes the key in a cleanup step. The target command does not inherit `OPENROUTER_MANAGEMENT_KEY`.

## Run With A Workspace-Scoped Ephemeral Key

1. If needed, list workspaces:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py workspaces
```

2. Dry-run the key request and command:

```bash
python3 skills/openrouter-ops/scripts/openrouter_ops.py run-ephemeral \
  --name "ariadne-smoke-YYYYMMDD" \
  --workspace "<workspace-id>" \
  --limit 1 \
  --expires-in-days 7 \
  -- uv run inspect eval ...
```

3. Run live only after the dry-run payload is right:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py run-ephemeral --live \
  --name "ariadne-smoke-YYYYMMDD" \
  --workspace "<workspace-id>" \
  --limit 1 \
  --expires-in-days 7 \
  -- uv run inspect eval ...
```

The script never prints or stores the runtime key. It deletes the runtime key by hash even when the target command fails. If cleanup fails, treat the key hash in the summary as an immediate revoke target.

Use `create-key` only to dry-run the OpenRouter payload shape. Live standalone creation is rejected because it would create an unusable orphan key.

## Key Inventory And Revocation

List keys for a workspace:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py list-keys \
  --workspace "<workspace-id>" \
  --include-disabled
```

Disable a key:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py disable-key --live \
  --hash "<key-hash>"
```

Delete a key:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py delete-key --live \
  --hash "<key-hash>"
```

## References

Read `references/openrouter-api.md` when adding or changing endpoints, payload fields, or ephemeral-key cleanup behavior.
