---
name: codex-thread-manager
description: Use when the user asks to create, open, start, rename, organize, or coordinate user-facing Codex app threads for a project, issue, workstream, or project/thread slug mapping.
---

# Codex Thread Manager

Use this skill to create real Codex app threads that the user can see in the Codex app, with consistent project-aware naming and a verified thread id.

## Core Rules

- Create user-facing app threads, not subagents, unless the user explicitly asks for subagents or delegation.
- Do not use `codex exec` for app threads. Use it only for explicit non-interactive report runs, and say it will not appear as a normal app thread.
- Do not use unverified deep links as a creation fallback.
- Prefer native app thread tools when available: `create_thread`, `list_threads`, `read_thread`, `send_message_to_thread`, `set_thread_title`.
- Native app thread tools are required for a user-visible Codex Desktop thread. If those tools are unavailable, say the tool surface cannot create a visible app thread in this session.
- The App Server protocol and bundled fallback script are diagnostic fallbacks only. They must not be treated as successful user-facing thread creation unless they verify a Desktop-visible source.
- Treat `cli`, `vscode`, and `appServer` thread sources as non-user-visible for Codex Desktop purposes.
- Verify creation before telling the user a thread exists.
- Keep the current/default model unless the user asks for a model override.
- Always request extra-high reasoning for user-requested project threads: with native `create_thread` / `send_message_to_thread`, pass `thinking: "xhigh"` explicitly; with the fallback script, pass `--effort xhigh` or rely on its `xhigh` default.

## Coordination Model

- Treat the parent conversation as the coordination surface unless the user chooses to continue in a child thread.
- Propose or create a dedicated Codex app thread when the work is long, repo-specific, tied to a Linear/GitHub issue, context-heavy, or likely to distract from the parent thread.
- Child thread prompts must be written in the same language as the parent conversation unless the user asks otherwise.
- Prompts may include a concise brief plus instructions to fetch more context through `workflow-context`; do not paste large context dumps when a bounded retrieval instruction is enough.
- Do not create heartbeat, reminder, monitor, or follow-up automation between threads unless the user explicitly asks for that automation.
- Do not substitute subagents, `codex exec`, or hidden background runs for user-facing Codex app threads.

## Naming

Use the stable identity model:

```text
area -> project -> workstream -> execution surface
```

Definitions:

- `area`: routing bucket for a workspace area.
- `project_slug`: durable context bucket for a project or temporary capture lane.
- `codex_project`: visible Codex app project/container name, formatted as `<area>-<project_slug>`.
- `workstream_slug`: active work slice inside the project.
- `execution surface`: Codex thread, Linear issue, git branch/worktree, vault note, PR, or deployment.

Closest project `AGENTS.md` files may override the visible thread title format when the Codex project/container already supplies the project identity. Follow those local overrides after resolving the target project.

Use this Codex app naming shape:

```text
Codex project/container: <area>-<project_slug>
Thread title inside it: <issue_id?>-<workstream_slug>
```

Examples:

```text
<area>-<project-slug> / <issue-id>-<workstream-slug>
<area>-<project-slug> / <workstream-slug>
<area>-<capture-slug> / <issue-id>-<workstream-slug>
```

Do not include `CoS` in visible thread titles. Coordinator state belongs in the prompt or status summary, not the thread identity.

Slug rules:

- Prefer existing repo, project, or registry names, normalized as short lowercase slugs.
- Lowercase, ASCII, hyphen-separated.
- Keep slugs short enough to scan in the thread list.
- Put issue identifiers at the start of the thread title when useful: `<issue_id>-<workstream_slug>`.
- If the Codex app surface does not expose a separate project/container field, encode both parts in the visible title as `<area>-<project_slug> - <issue_id?>-<workstream_slug>`.
- Do not create a new project slug if an obvious repo, Linear project, or vault project already exists.
- Use `capture` for a single action, rough idea, or item that does not yet deserve durable project memory.
- Promote from `capture/<workstream>` to a dedicated `project_slug` only when the work gains recurring context, multiple issues/threads, source materials, decisions, or ownership.

## Issue And Repo Coordination

- For issue-linked threads, ensure the issue identifier is present in both the thread title and the initial prompt.
- Do not write the Codex thread id back to the issue by default. Do that only when the user explicitly asks for issue-visible thread provenance.
- For repo-scoped threads, include a prompt instruction to read the relevant `AGENTS.md` files and evaluate whether the repo's workflow layer applies.
- When a repo uses Ora et Labora or a comparable local workflow, tell the child thread to use the minimal relevant subset; do not force the full lifecycle for small documentation, policy, or context-only edits.

## Project Resolution

Before creating a thread:

1. Resolve the target `cwd` to the project root or the active worktree.
2. Read the closest relevant `AGENTS.md` files if the prompt delegates implementation.
3. If the request maps to a Linear issue, include the issue id and URL in the prompt, but mutate Linear only when explicitly asked.
4. If the target needs a branch/worktree, use the workspace's worktree workflow first; do not create an app thread pointed at the wrong checkout.
5. If routing is ambiguous, ask one concise question instead of creating a thread in the wrong project.

## Prompt Contract

A project thread prompt should include:

- area, project slug, Codex project/container name, and workstream slug
- target cwd and project/repo name
- objective in one or two concrete paragraphs
- relevant branch/worktree/issue context
- allowed and forbidden actions
- verification expectations
- final status format

Native tool calls must include `thinking: "xhigh"` for user-requested project threads unless the user explicitly asks for a lower effort. Do not rely on the app default for reasoning effort.

Example native creation shape:

```json
{
  "target": {"type": "project", "projectId": "/path/to/workspace", "environment": {"type": "local"}},
  "thinking": "xhigh",
  "prompt": "..."
}
```

When a child thread should retrieve context itself, include a focused instruction such as:

```text
Use workflow-context to fetch the relevant Linear issue, project registry entry, vault/project notes, and repo state before proposing changes. Keep the retrieval bounded to the objective.
```

For implementation threads, include this default guardrail:

```text
Do not commit, push, open PRs, deploy, send messages, mutate external systems, spawn subagents, or start heartbeat/monitor automations unless explicitly asked. If blocked, explain the exact blocker instead of guessing.
```

For read-only research/review threads, set read-only intent clearly and tell the thread not to edit files.

## Creation Workflow

1. Prefer native app thread tools if the tool surface exposes them.
2. If native tools are not exposed, do not create a subagent, `codex exec` run, CLI session, or App Server session as a substitute for a visible app thread.
3. Use `scripts/create_app_thread.mjs` only when a diagnostic fallback is explicitly useful, then require `verified: true` and `visible: true`.
4. If the fallback returns `verified: false`, `visible: false`, or a non-user-visible `source`, report that no visible Codex app thread was created. Include the attempted title, cwd, and verification error.
5. Report the thread title, id, cwd, and whether the turn completed or is still running only after user-visible verification succeeds.

Example fallback command:

```bash
node skills/codex-thread-manager/scripts/create_app_thread.mjs \
  --cwd <project-root-or-worktree> \
  --title "<area>-<project_slug> - <issue_id>-<workstream_slug>" \
  --prompt-file /tmp/thread-prompt.md \
  --effort xhigh \
  --sandbox workspace-write \
  --approval never
```

Use `--sandbox read-only` for pure planning/review. Use `--approval on-request` when the thread may need user-confirmed actions. Use `--approval never` only when the prompt forbids high-impact actions and the sandbox is appropriately scoped.

## Verification

Treat a thread as created only after at least one of these succeeds:

- native thread tool returns a thread id and a later list/read confirms the title
- fallback script prints both `verified: true` and `visible: true`
- App Server `thread/list` finds the same id and title with a source that is not `cli`, `vscode`, or `appServer`

If verification fails, say so and do not claim the thread is visible. If a fallback created a non-visible session, call it an App Server diagnostic session, not a Codex app thread.

## Updating The Procedure

When the live Codex app behavior differs from this skill, trust the official OpenAI Codex docs and the live tool surface. Update the canonical skill in `ars-operandi`; update root `AGENTS.md` only for workspace-wide policy changes, and sync any installed workspace copy separately.
