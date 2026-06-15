---
name: openrouter-ops
description: Use when Codex needs to operate OpenRouter safely: create workspace-scoped runtime API keys with spending limits and expirations, list workspaces or keys, disable/delete/rotate keys, store runtime keys in 1Password, or run project commands with OPENROUTER_API_KEY through 1Password secret references without exposing secrets.
---

# openrouter-ops

Use this skill for OpenRouter provider operations from Codex. Keep OpenRouter general: Ariadne, Inspect, or any other project may consume the runtime key, but this skill owns the provider-operation boundary.

## Security Rules

- Do not ask the user to paste OpenRouter keys in chat.
- Do not print, log, commit, or save plaintext keys.
- Treat Management API keys and runtime API keys as different credentials.
- Bootstrap Management API keys manually in the OpenRouter UI, then store them in 1Password.
- Use 1Password as the trust store. Prefer `op run` secret references over `.env` plaintext.
- Use `--live` only when the user explicitly wants a real OpenRouter or 1Password mutation.
- Use workspace IDs for key creation. Resolve a workspace slug with `workspaces` or `workspace` first.

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

For runtime use, pass a runtime key reference into the target command:

```bash
python3 skills/openrouter-ops/scripts/openrouter_ops.py run-with-key \
  --key-ref "op://<vault>/<runtime-key-item>/credential" \
  -- uv run inspect eval ...
```

`op run` masks secrets in subprocess output by default. Do not add `--no-masking`.

## Create A Workspace-Scoped Key

1. If needed, list workspaces:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py workspaces
```

2. Dry-run the key request:

```bash
python3 skills/openrouter-ops/scripts/openrouter_ops.py create-key \
  --name "ariadne-smoke-YYYYMMDD" \
  --workspace "<workspace-id>" \
  --limit 1 \
  --expires-in-days 7 \
  --op-vault "<vault>" \
  --op-item "OpenRouter runtime - ariadne smoke - YYYYMMDD"
```

3. Create and store the key only after the dry-run payload is right:

```bash
OPENROUTER_MANAGEMENT_KEY="op://<vault>/<management-item>/credential" \
op run -- python3 skills/openrouter-ops/scripts/openrouter_ops.py create-key --live \
  --name "ariadne-smoke-YYYYMMDD" \
  --workspace "<workspace-id>" \
  --limit 1 \
  --expires-in-days 7 \
  --op-vault "<vault>" \
  --op-item "OpenRouter runtime - ariadne smoke - YYYYMMDD"
```

The script stores the one-time runtime key in 1Password before printing success. If 1Password storage fails after OpenRouter key creation, it attempts to delete the newly created key by hash and reports the redacted cleanup result.

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

Read `references/openrouter-api.md` when adding or changing endpoints, payload fields, or 1Password storage behavior.
