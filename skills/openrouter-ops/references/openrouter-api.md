# OpenRouter Ops Reference

Use official OpenRouter docs as the source of truth before changing endpoint behavior.

Core endpoints used by this skill:

- `GET /api/v1/key`: inspect the current authenticated key.
- `GET /api/v1/workspaces`: list workspaces. Management key required.
- `GET /api/v1/workspaces/:id`: get a workspace by ID or slug. Management key required.
- `GET /api/v1/keys`: list API keys, optionally filtered by `workspace_id`. Management key required.
- `POST /api/v1/keys`: create a runtime API key. Management key required.
- `PATCH /api/v1/keys/:hash`: update or disable a runtime API key. Management key required.
- `DELETE /api/v1/keys/:hash`: delete a runtime API key. Management key required.

Create-key request fields currently supported by the CLI:

- `name`: required.
- `workspace_id`: optional UUID. Defaults to the OpenRouter default workspace when omitted.
- `expires_at`: optional UTC ISO 8601 timestamp.
- `include_byok_in_limit`: optional boolean.
- `limit`: optional USD spending limit.
- `limit_reset`: optional `daily`, `weekly`, `monthly`, or `null`.

OpenRouter returns the created runtime key once. The CLI must store it in 1Password before reporting success, and must not print the secret.

1Password storage:

- Use `API_CREDENTIAL` items.
- Put the runtime key in the concealed `credential` field.
- Put non-secret OpenRouter metadata in notes: hash, label, workspace, limit, reset, expiration.
- Use secret references plus `op run` for command execution.
- Do not pass secret values as `op item create` assignment arguments; use a JSON template over stdin.
