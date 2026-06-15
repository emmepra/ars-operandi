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

OpenRouter returns the created runtime key once. The CLI must keep it in memory only long enough to inject it into the target subprocess, and must not print or persist the secret.

Ephemeral runtime-key handling:

- Management keys may live in 1Password and be resolved with `op run`.
- Runtime keys are created with `POST /keys`, passed as `OPENROUTER_API_KEY` to one subprocess, then deleted with `DELETE /keys/:hash`.
- The subprocess environment should not inherit `OPENROUTER_MANAGEMENT_KEY`.
- Capture and redact subprocess output before re-emitting it, because dynamically created runtime keys are not masked by `op run`.
- If deletion fails, report only the non-secret key hash and cleanup error so an operator can revoke it manually.
