# Ars Operandi AGENTS Guide

Scope: applies to this repository.

## Purpose

Ars Operandi is a public skill pack for operational adapters. Keep it focused on external operating surfaces that agents need to use reliably, such as Railway, Cloudflare, Hetzner, Docker Compose VPS, Tailscale, DNS, storage, email, Codex app thread management, and related deployment or operations workflows.

Ora et Labora remains the workflow layer. Ars Operandi is the provider-adapter layer.

## Routing

- Use `railway-deploy` for Railway deploys, service links, variables, domains, public smoke checks, and rollback notes.
- Use `codex-thread-manager` for user-facing Codex app thread creation, naming, verification, and project/workstream thread coordination.
- Use Ora et Labora workflow skills for issue shaping, branch/worktree handling, verification, PRs, releases, and state discipline.
- Add new operational adapter skills only when the surface has recurring commands, auth boundaries, failure modes, naming rules, or verification steps that are worth reusing.

## Public Repo Policy

- Do not commit `.project/**`, local task state, raw browser evidence, credentials, `.env`, provider tokens, cookies, private domains, or private customer/project data.
- Keep provider skill examples generic or sanitized.
- Prefer provider-native secret storage and CLI auth. Skill files must never contain real tokens.
- Keep `SKILL.md` files concise; move provider-specific detail into `references/` only when needed.
- Use scripts for deterministic read-only snapshots or validation helpers.

## Skill Shape

Each skill should include:

- `SKILL.md`
- `agents/openai.yaml` when useful for UI metadata
- `references/` for deeper provider-specific guidance
- `scripts/` for deterministic helpers

Do not add subfolder README files inside individual skills unless there is a clear public-user need.

## Verification

Before committing skill changes:

```bash
python scripts/validate_skills.py
```

For provider scripts, prefer read-only dry runs by default and redact secrets from output.
