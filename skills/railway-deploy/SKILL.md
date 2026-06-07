---
name: railway-deploy
description: Use when deploying, releasing, debugging, or verifying applications and services on Railway, especially when branch, service, environment, database, variables, domain, or smoke-test state may be ambiguous.
---

# railway-deploy

Use this skill for Railway release and deployment work where the current repo, branch, service, environment, or production state must be verified before acting.

Core principle: do not confuse a merge, a Railway deployment, and a public smoke check. A Railway task is not complete until the target service, commit, variables, deployment status, and user-facing URL are known.

## Credential Rule

Credentials never belong in this skill, in chat, in commit history, or in logs.

The Railway CLI auth for interactive Codex work should be global for the OS user on that host, not project-local. Treat `~/.railway/config.json` as the persistent CLI credential reference; do not print, copy, commit, or summarize its contents.

Before asking the user to re-authenticate:

- run `railway whoami` from any directory to check the global CLI session
- if the target repo is already linked, run `railway status --json` from the repo root to confirm project/service/environment
- if the CLI reports `invalid_grant` or `Unauthorized`, upgrade the CLI if it is stale, then run `railway login --browserless` once and ask the user to complete the activation code
- keep `~/.railway/` private and `~/.railway/config.json` at mode `600`

Before touching deployed secrets, read `references/credentials.md`. In short:

- use persistent Railway CLI auth for the OS user running Codex; log in once per host with `railway login` / `railway login --browserless`
- use Railway Variables for deployed app secrets
- use Railway reference variables for service links such as `DATABASE_URL=${{Postgres.DATABASE_URL}}`
- use CI secret storage for `RAILWAY_API_TOKEN` or project tokens
- inventory variable names and required presence, not secret values
- never print `.env`, Railway variable values, tokens, cookies, or database URLs

## Required Preflight

Run preflight before any mutating Railway command.

1. Load local instructions.
   - Read the closest `AGENTS.md`.
   - If the project uses `dev -> main` releases, use `release-train` before production deploys.
   - Use `verify-and-evidence` for verification selection and evidence.
2. Confirm repo and branch state.
   - `git status --short`
   - `git branch --show-current`
   - `git remote -v`
   - `git fetch --all --prune` when network is available and branch state matters
   - Inspect `origin/dev` and `origin/main` when production deploys from stable branches.
3. Confirm Railway context.
   - `railway status` or `railway status --json`
   - Confirm workspace, project, environment, linked service, and public domain.
   - If local Railway config is missing or stale, link explicitly with `railway link`.
4. Determine deployment mode.
   - GitHub integration auto-deploy from a branch
   - CLI deploy with `railway up`
   - template/database deploy with `railway deploy` or `railway add --database`
5. Identify runtime contract.
   - build/start command
   - Dockerfile or `railway.json`
   - healthcheck path
   - `PORT` binding requirements
   - database/service dependencies
   - required variables by name only

Use `scripts/railway_snapshot.sh` for a read-only context snapshot when useful.

## Production Gate

Do not mutate production unless one of these is true:

- the user explicitly asked to deploy, release, link, set variables, create services, or proceed end-to-end
- the current task is already an approved production/deploy issue
- the action is a non-mutating read, status check, or public smoke check

If production impact is ambiguous, stop and ask a short confirmation. This includes `railway up`, `railway redeploy`, variable changes, service creation/deletion, domain changes, and database operations.

## Deploy Procedure

1. Prepare the code path.
   - Use a clean checkout or scoped worktree.
   - Do not deploy from a dirty branch unless the user explicitly wants the exact dirty state deployed.
   - If production deploys from `main`, make sure the release PR or merge to `main` has happened before deploying.
2. Run project checks.
   - Use the repo's package manager and documented test/build commands.
   - For frontend or full-stack apps, run a local build and at least one local smoke check when practical.
3. Repair Railway auth only when needed.
   - Reuse the host's existing Railway CLI login whenever possible.
   - If reads work but mutations fail with `Unauthorized`, use `railway login --browserless` once on that host and retry once.
   - Do not paste or store tokens.
4. Deploy deliberately.
   - Code deploy: prefer `railway up --message "<scope> <commit>"` from the intended checkout.
   - Template/database deploy: use the Railway command that matches the resource, then verify the created service.
   - For Postgres in the same project, prefer a separate database service and reference variables rather than copying raw connection strings.
5. Watch deployment state.
   - Use `railway deployment`, `railway status`, `railway service status`, or the project dashboard as supported by the installed CLI.
   - If CLI syntax differs, run `railway <command> --help` and adapt.

## Postgres And Variables

For an app that needs Postgres:

- create or identify the Postgres service first
- set app-side `DATABASE_URL` as a reference to the Postgres service when possible
- avoid public TCP proxy unless external database access is explicitly needed
- verify migrations/schema requirements before claiming the app is ready

For variables:

- compare required variable names from code/docs against Railway variables
- do not dump variable values
- if a value must be changed, state the variable name and target service/environment before changing it
- use Railway UI or CLI variable commands; do not commit `.env`

## Verification

A Railway deploy is not done until evidence covers the deployed surface.

Minimum completion evidence:

- Railway project, service, and environment
- deployment method and commit/branch
- public URL or domain
- deployment status
- health endpoint result when available
- one user-facing route or API smoke relevant to the app
- database/link status when persistence is involved
- rollback path

For browser-visible apps, use browser verification or Playwright when the claim depends on UI behavior. Keep evidence concise; do not paste full logs unless failure text matters.

## Rollback Notes

Always state the concrete rollback path:

- revert release merge commit
- redeploy previous Railway deployment
- restore previous variable value from the user/secret store
- disconnect or recreate database reference
- disable domain/route only if that is the intended rollback

Do not write "rollback normally" without an actionable command or dashboard action.

## Stop Conditions

Stop and report instead of pushing forward when:

- repo or Railway project identity is uncertain
- the deployed branch differs from the branch the user expects
- production requires a release PR that has not happened
- required secrets are missing and the user has not provided them through a secure channel
- database migration status is unknown for a persistence-changing release
- public smoke fails
- Railway auth keeps returning `Unauthorized` after one fresh login
- a command would reveal or store credentials

## Completion Summary

Report:

- selected Railway workspace/project/service/environment
- branch/commit deployed or verified
- variables checked by name, not value
- commands/checks run and verdicts
- public smoke URL and result
- rollback note
- any skipped check and residual risk
