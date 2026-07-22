# Ars Operandi

Ars Operandi is a public Codex skill pack for operational adapters: deployment platforms, hosting providers, DNS, edge services, VPS runtimes, Codex app thread management, and other external operating surfaces.

It complements Ora et Labora. Ora et Labora defines the repo-first workflow: issues, branches, worktrees, PRs, verification, releases, and rollback discipline. Ars Operandi defines provider-specific operating procedures that an agent should use inside that workflow.

## Skills

| Skill | Use for |
| --- | --- |
| `codex-thread-manager` | User-facing Codex app thread creation, naming, verification, and project/workstream coordination |
| `mailctl-email-access` | Fail-closed, bounded GWS or Proton metadata access through a consumer-provided `mailctl` runtime |
| `official-linear-mcp-bridge` | Temporary transparent STDIO transport to isolated connections of the official Linear MCP |
| `openrouter-ops` | OpenRouter workspace/key operations, ephemeral runtime key injection, and safe revoke/rotate workflows |
| `railway-deploy` | Railway deployment, release, variables, services, Postgres links, domains, smoke checks, and rollback notes |

Future adapters may cover Cloudflare, Hetzner, Docker Compose VPS, Tailscale, Resend, and other operational surfaces.

Ars Operandi is the durable owner of the public mail operating surface. Until a
separate atomic cutover transfers the runtime and tests here, Workflow Agent is
only the transitional `mailctl` source; none of its other subsystems are part of
the mail surface or installation contract.

## Usage

Copy a skill folder into the user skill directory, or use a skill-specific installer when one is provided.

```bash
cp -R skills/railway-deploy ~/.codex/skills/
cp -R skills/codex-thread-manager ~/.codex/skills/
cp -R skills/mailctl-email-access ~/.codex/skills/
cp -R skills/openrouter-ops ~/.codex/skills/
```

`official-linear-mcp-bridge` includes a dry-run-first installer for explicitly
named, independently authenticated connections to the official Linear MCP. See
its [installation reference](skills/official-linear-mcp-bridge/references/installation-and-operations.md).
Do not manually copy it and separately register competing MCP configuration.
The bridge forwards the upstream catalog, annotations, calls, and results without
defining Linear tools. Project-to-alias routing and identity policy remain owned
by the consumer and must fail closed; Ars Operandi does not define consumer
projects, aliases, or accounts.

Invoke explicitly when needed:

```text
Use $railway-deploy to deploy this app on Railway.
Use $mailctl-email-access for bounded provider-aware metadata from one explicit consumer route.
Use $official-linear-mcp-bridge to plan isolated connections to the official Linear MCP.
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
