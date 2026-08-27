# Ars Operandi

Ars Operandi is a public Codex skill pack for operational adapters: deployment platforms, hosting providers, DNS, edge services, VPS runtimes, Codex app thread management, and other external operating surfaces.

It complements Ora et Labora. Ora et Labora defines the repo-first workflow: issues, branches, worktrees, PRs, verification, releases, and rollback discipline. Ars Operandi defines provider-specific operating procedures that an agent should use inside that workflow.

## Skills

| Skill | Use for |
| --- | --- |
| `codex-thread-manager` | User-facing Codex app thread creation, naming, verification, and project/workstream coordination |
| `mailctl-email-access` | Fail-closed, bounded GWS or Proton search, selected content, and attachment access through the canonical Ars mail MCP and CLI |
| `official-linear-mcp-bridge` | Two native official Linear OAuth/keyring aliases, exact staged migration, bounded recovery, restore, and finalization |
| `openrouter-ops` | OpenRouter workspace/key operations, ephemeral runtime key injection, and safe revoke/rotate workflows |
| `railway-deploy` | Railway deployment, release, variables, services, Postgres links, domains, smoke checks, and rollback notes |

Future adapters may cover Cloudflare, Hetzner, Docker Compose VPS, Tailscale, Resend, and other operational surfaces.

Ars Operandi owns the canonical provider-aware mail runtime, tests, public
skill, and dry-run-first installer. Normal Codex reads use one Mac-local
`ars-mail` MCP process; Proton credentials are resolved once per process and
retained only in RAM. Workflow Agent may remain only as an inactive transitional
source until the consumer performs the documented atomic cutover; none of its
other subsystems belong to this surface.

## Usage

Copy a skill folder into the user skill directory, or use a skill-specific installer when one is provided.

```bash
cp -R skills/railway-deploy ~/.codex/skills/
cp -R skills/codex-thread-manager ~/.codex/skills/
cp -R skills/openrouter-ops ~/.codex/skills/
```

`official-linear-mcp-bridge` retains its legacy name so its dry-run-first installer can migrate or recover only proven prior state before retiring the old runtime. See its [installation reference](skills/official-linear-mcp-bridge/references/installation-and-operations.md); do not manually rewrite the managed Linear config.

`mailctl-email-access` includes the canonical runtime in this repository and a
dry-run-first installer for the skill plus the fixed `ars-mail` MCP alias. See
its [runtime and installation reference](skills/mailctl-email-access/references/runtime-and-installation.md). Do not register a competing mail MCP or copy the runtime into another repository.

Invoke explicitly when needed:

```text
Use $railway-deploy to deploy this app on Railway.
Use $mailctl-email-access for bounded provider-aware mail from one explicit consumer route.
```

## Credential Policy

This repository does not contain credentials.

Provider auth should live in the provider CLI's normal per-host login state, a CI secret store, or the provider's own variable/secrets system. Skills may describe where credentials belong, but must not include tokens, `.env` values, cookies, database URLs, or copied secret material.

## Relationship To Ora et Labora

Use Ora et Labora skills for the workflow phase:

- `issue-shaping` for scope and acceptance criteria
- `worktree-flow` for branches, worktrees, and PRs
- `verify-and-evidence` for verification and evidence
- `release-train` for grouped `dev` to `main` promotion

Use Ars Operandi skills when a workflow phase touches a provider-specific runtime.

## Development

Validate skill frontmatter and required files:

```bash
python scripts/validate_skills.py
```

Workflow examples live under `.github/workflow-examples/`. They are intentionally inert until copied into `.github/workflows/` by a maintainer with a GitHub token that has workflow permissions.
