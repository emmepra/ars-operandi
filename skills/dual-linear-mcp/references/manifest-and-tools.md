# Manifest and tool contract

## Routing chain

The adapter consumes an external YAML manifest; it does not own or rewrite it.

```text
project key/name/alias
  -> project.tracking
  -> tracking_profiles.<profile>.portfolio.connection
  -> linear_connections.<connection>
  -> credential_profile + expected_workspace_id
```

Use [example-manifest.yaml](example-manifest.yaml) as a sanitized fixture. Extra consumer-owned top-level and project fields are ignored. The adapter requires these fields:

- `linear_connections.<alias>`: `adapter`, `credential_profile`, `expected_workspace`, `binding_state`, and `expected_workspace_id` when verified.
- `tracking_profiles.<name>.portfolio`: `type: linear` and one known `connection`.
- `projects[]`: `key`, `tracking`, optional `name`, `aliases`, and `linear_project_id`.

Only `binding_state: verified` can resolve. `planned`, `blocked`, missing references, unknown selectors, and ambiguous selectors fail closed. Supplying both a project and connection alias also fails. The CLI-only `bootstrap` path may inspect one explicitly selected `planned` connection, but its candidate output never changes routing eligibility.

The `adapter` value must be `dual-linear-mcp` for generic manifests or `cerebro` for the compatible Cerebro Project Index contract. Other adapter types fail validation instead of being routed accidentally.

## Tools

| Tool | Class | Guard |
| --- | --- | --- |
| `resolve_linear_route` | Read-only, offline | Exactly one verified route |
| `linear_discover` | Read-only, live | Credential workspace ID must match manifest |
| `linear_get_issue` | Read-only, live | Workspace plus returned team/project identity |
| `linear_create_issue` | Mutation | Server flag, `confirm=true`, workspace/team preflight, read-back |
| `linear_update_issue` | Mutation | Server flag, `confirm=true`, issue preflight, read-back |

There is no delete, generic mutation, generic GraphQL, or fallback tool.

`bootstrap` is a read-only CLI command, not an MCP tool. It requires one or more explicit connection aliases, rejects blocked aliases, preloads only their credential profiles, and never writes the consumer manifest. A connection with an existing stable ID must match it; otherwise the command returns a candidate that still requires independent operator confirmation.

## Identity and read-back

Before every mutation, the adapter uses the selected credential to fetch the Linear organization and all accessible teams. It compares the organization ID with `expected_workspace_id` and requires the target issue team to be in that identity response.

For a project route, `linear_project_id` is the expected project binding. Create uses that value unless no binding exists; a conflicting caller value is rejected before mutation. Update first reads the issue and checks its team and project identity.

After create or update, the adapter performs a separate issue read through the same selected credential. It verifies team, optional project binding, and every supported changed field. A read-back failure reports the mutation as unverified; it never retries through a different connection.

## Error and redaction model

Tool failures return a stable error code and a sanitized message. Runtime secrets are registered with an in-memory exact-value redactor. GraphQL error messages and response bodies are suppressed; only safe status or extension codes may be returned.

Treat these codes as hard stops: `unknown_project`, `unknown_connection`, `ambiguous_project`, `connection_not_verified`, `connection_blocked`, `workspace_identity_mismatch`, `team_identity_mismatch`, `project_identity_mismatch`, `mutations_disabled`, `mutation_confirmation_required`, `operation_not_allowed`, and `read_back_failed`.
