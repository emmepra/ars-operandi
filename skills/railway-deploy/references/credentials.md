# Railway Credentials And Secrets

Use this reference whenever Railway work touches auth, `.env`, variables, database URLs, or CI tokens.

## Where Credentials Belong

| Credential type | Storage location |
| --- | --- |
| Local Codex/Railway work | Railway CLI login state for the OS user running Codex on that host |
| Production app secrets | Railway Variables on the target service/environment |
| Database connection inside Railway | Railway reference variable, for example `DATABASE_URL=${{Postgres.DATABASE_URL}}` |
| CI deploy token | CI secret store, commonly exposed to the job as `RAILWAY_API_TOKEN` |
| Local development env | Untracked `.env` with restrictive permissions |
| Shared secrets for humans | A password manager or approved secret store, not chat or git |

## Recommended Codex Pattern

For interactive Codex work, prefer persistent Railway CLI auth per execution host:

- Mac Codex uses the Mac user's Railway CLI login state.
- Pi Codex uses the Pi user's Railway CLI login state.
- Log in once per host with `railway login` or `railway login --browserless`.
- Railway CLI stores the persistent session under `~/.railway/`, commonly `~/.railway/config.json`; use that as the credential reference, not as content to inspect or copy.
- Keep the Railway config directory private to the user, for example `chmod -R go-rwx ~/.railway`.
- Do not put Railway tokens in `SKILL.md`, `~/.codex/config.toml`, repo files, `.env`, or memory notes.
- Before asking for a new login, run `railway whoami` from any directory. If the repo is linked, follow with `railway status --json` from the repo root.
- If auth is stale (`invalid_grant`, `Unauthorized`, or mutation-only failures), update the CLI if needed, then run `railway login --browserless` once and retry.

This gives the desired "login once and reuse it" behavior without making the credential portable across unrelated agents or repositories. If the host is rebuilt, log in again. If the host is lost or compromised, revoke the Railway session/token from Railway.

Use `RAILWAY_API_TOKEN` only for non-interactive CI or deliberately isolated automation. Store it in the CI/secret manager, not in Codex instructions.

## Agent Handling Rules

- Do not print secret values.
- Do not read `.env` contents unless the user explicitly asks and the output will be redacted.
- Prefer checking required variable names from code, docs, examples, and deployment errors.
- When listing variables, redact values or avoid commands that print values.
- Do not store tokens in shell history, notes, issue bodies, PR bodies, logs, or skill files.
- Do not add `.env` files to git.
- Do not copy a raw database URL between services when a Railway reference variable can be used.
- If the user provides a token, use it only for the current operation and do not persist it unless the user explicitly asks where to store it.

## Secure Patterns

Local interactive work:

```bash
railway login
railway login --browserless
chmod -R go-rwx ~/.railway
chmod 600 ~/.railway/config.json
railway whoami
railway status
```

CI work:

```bash
export RAILWAY_API_TOKEN="$RAILWAY_API_TOKEN"
railway up --ci
```

Postgres app link inside one Railway project:

```bash
railway variable set 'DATABASE_URL=${{Postgres.DATABASE_URL}}'
```

Use the real service name if the database service is not named `Postgres`.

## Red Flags

- `.env` committed or staged
- `DATABASE_URL=` pasted into chat, issue text, PR body, logs, or command output
- `railway variables` output copied without redaction
- using a public TCP proxy for a database without an explicit external-access requirement
- one account-level token reused across unrelated repos or CI jobs
- production mutation after a stale CLI auth failure without a fresh login check
