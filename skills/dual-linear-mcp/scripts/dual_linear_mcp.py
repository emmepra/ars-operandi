#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.27,<2", "pyyaml>=6,<7"]
# ///
"""Fail-closed Linear MCP adapter driven by an explicit project manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeVar


LINEAR_ENDPOINT = "https://api.linear.app/graphql"
ALLOWED_BINDING_STATES = {"planned", "verified", "blocked"}
SUPPORTED_ADAPTERS = {"cerebro", "dual-linear-mcp"}
ALLOWED_MUTATIONS = frozenset({"issue_create", "issue_update"})
TOOL_NAMES = frozenset(
    {
        "resolve_linear_route",
        "linear_discover",
        "linear_get_issue",
        "linear_create_issue",
        "linear_update_issue",
    }
)


class AdapterError(RuntimeError):
    """Expected fail-closed error with a stable, safe code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def safe_payload(self, redactor: "SecretRedactor") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": redactor.redact_text(self.message),
        }
        if self.details:
            payload["details"] = redactor.redact(self.details)
        return payload


class SecretRedactor:
    """Redact exact runtime secrets without persisting them."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.RLock()

    def register(self, value: str) -> None:
        if value:
            with self._lock:
                self._secrets.add(value)

    def redact_text(self, value: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "<redacted-secret>")
        redacted = re.sub(
            r"(?i)(authorization|api[_-]?key|token|secret)(\s*[:=]\s*)[^\s,;]+",
            r"\1\2<redacted-secret>",
            redacted,
        )
        return redacted

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in {
                    "authorization",
                    "api_key",
                    "apikey",
                    "token",
                    "secret",
                }:
                    result[str(key)] = "<redacted-secret>"
                else:
                    result[str(key)] = self.redact(item)
            return result
        if isinstance(value, (list, tuple, set)):
            return [self.redact(item) for item in value]
        return value


def safe_tool_call(
    redactor: SecretRedactor, action: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Return a structured tool result without leaking exception contents."""

    try:
        result = redactor.redact(action())
        return {"ok": True, **result}
    except AdapterError as exc:
        return {"ok": False, "error": exc.safe_payload(redactor)}
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": "The adapter failed without exposing runtime details.",
            },
        }


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - inline uv metadata supplies it
        raise AdapterError(
            "dependency_missing",
            "PyYAML is required; run the script through uv.",
        ) from exc

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdapterError(
            "manifest_unavailable",
            "The explicit manifest path could not be read.",
            details={"path": str(path)},
        ) from exc
    except yaml.YAMLError as exc:
        raise AdapterError(
            "invalid_manifest", "The manifest is not valid YAML."
        ) from exc
    if not isinstance(data, dict):
        raise AdapterError("invalid_manifest", "The manifest root must be a mapping.")
    return data


def _normalize(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AdapterError("invalid_manifest", f"{label} must be a mapping.")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError("invalid_manifest", f"{label} must be a non-empty string.")
    return value.strip()


@dataclass(frozen=True)
class LinearConnection:
    alias: str
    adapter: str
    credential_profile: str
    expected_workspace: str
    expected_workspace_id: str | None
    binding_state: str


@dataclass(frozen=True)
class ProjectBinding:
    key: str
    name: str
    aliases: tuple[str, ...]
    tracking_profile: str
    expected_project_id: str | None

    def selectors(self) -> frozenset[str]:
        return frozenset(
            _normalize(value) for value in (self.key, self.name, *self.aliases) if value
        )


@dataclass(frozen=True)
class ResolvedRoute:
    connection_alias: str
    credential_profile: str
    expected_workspace: str
    expected_workspace_id: str
    project_key: str | None
    expected_project_id: str | None

    def public_payload(self) -> dict[str, Any]:
        return {
            "connection_alias": self.connection_alias,
            "expected_workspace": self.expected_workspace,
            "expected_workspace_id": self.expected_workspace_id,
            "project_key": self.project_key,
            "expected_project_id": self.expected_project_id,
            "binding_state": "verified",
        }


@dataclass(frozen=True)
class Manifest:
    connections: Mapping[str, LinearConnection]
    tracking_connections: Mapping[str, str]
    projects: tuple[ProjectBinding, ...]
    source_path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        resolved = path.expanduser().resolve()
        return cls.from_data(load_yaml(resolved), source_path=resolved)

    @classmethod
    def from_data(
        cls,
        data: Mapping[str, Any],
        *,
        source_path: Path | None = None,
    ) -> "Manifest":
        raw_connections = _mapping(data.get("linear_connections"), "linear_connections")
        if not raw_connections:
            raise AdapterError(
                "invalid_manifest", "linear_connections must not be empty."
            )

        connections: dict[str, LinearConnection] = {}
        normalized_connection_aliases: dict[str, str] = {}
        for raw_alias, raw_value in raw_connections.items():
            alias = _required_string(raw_alias, "linear connection alias")
            value = _mapping(raw_value, f"linear_connections.{alias}")
            binding_state = _required_string(
                value.get("binding_state"), f"linear_connections.{alias}.binding_state"
            )
            if binding_state not in ALLOWED_BINDING_STATES:
                raise AdapterError(
                    "invalid_manifest",
                    f"linear_connections.{alias}.binding_state must be planned, verified, or blocked.",
                )
            workspace_id = value.get("expected_workspace_id")
            if workspace_id is not None:
                workspace_id = _required_string(
                    workspace_id, f"linear_connections.{alias}.expected_workspace_id"
                )
            if binding_state == "verified" and not workspace_id:
                raise AdapterError(
                    "invalid_manifest",
                    f"linear_connections.{alias} requires expected_workspace_id when verified.",
                )
            normalized = _normalize(alias)
            if normalized in normalized_connection_aliases:
                raise AdapterError(
                    "invalid_manifest", "Linear connection aliases must be unambiguous."
                )
            normalized_connection_aliases[normalized] = alias
            adapter = _required_string(
                value.get("adapter"), f"linear_connections.{alias}.adapter"
            )
            if adapter not in SUPPORTED_ADAPTERS:
                raise AdapterError(
                    "invalid_manifest",
                    f"linear_connections.{alias}.adapter is not supported.",
                )
            connections[alias] = LinearConnection(
                alias=alias,
                adapter=adapter,
                credential_profile=_required_string(
                    value.get("credential_profile"),
                    f"linear_connections.{alias}.credential_profile",
                ),
                expected_workspace=_required_string(
                    value.get("expected_workspace"),
                    f"linear_connections.{alias}.expected_workspace",
                ),
                expected_workspace_id=workspace_id,
                binding_state=binding_state,
            )

        raw_profiles = _mapping(data.get("tracking_profiles"), "tracking_profiles")
        tracking_connections: dict[str, str] = {}
        for raw_name, raw_value in raw_profiles.items():
            name = _required_string(raw_name, "tracking profile name")
            value = _mapping(raw_value, f"tracking_profiles.{name}")
            portfolio = _mapping(
                value.get("portfolio"), f"tracking_profiles.{name}.portfolio"
            )
            if portfolio.get("type") != "linear":
                raise AdapterError(
                    "invalid_manifest",
                    f"tracking_profiles.{name}.portfolio.type must be linear.",
                )
            connection_alias = _required_string(
                portfolio.get("connection"),
                f"tracking_profiles.{name}.portfolio.connection",
            )
            if connection_alias not in connections:
                raise AdapterError(
                    "invalid_manifest",
                    f"tracking_profiles.{name} references an unknown Linear connection.",
                )
            tracking_connections[name] = connection_alias

        raw_projects = data.get("projects")
        if not isinstance(raw_projects, list) or not raw_projects:
            raise AdapterError("invalid_manifest", "projects must be a non-empty list.")
        projects: list[ProjectBinding] = []
        project_keys: set[str] = set()
        for index, raw_value in enumerate(raw_projects):
            value = _mapping(raw_value, f"projects[{index}]")
            key = _required_string(value.get("key"), f"projects[{index}].key")
            if key in project_keys:
                raise AdapterError("invalid_manifest", "Project keys must be unique.")
            project_keys.add(key)
            tracking = _required_string(
                value.get("tracking"), f"projects[{index}].tracking"
            )
            if tracking not in tracking_connections:
                raise AdapterError(
                    "invalid_manifest",
                    f"Project {key} references an unknown tracking profile.",
                )
            raw_aliases = value.get("aliases", [])
            if not isinstance(raw_aliases, list) or not all(
                isinstance(item, str) and item.strip() for item in raw_aliases
            ):
                raise AdapterError(
                    "invalid_manifest",
                    f"Project {key} aliases must be non-empty strings.",
                )
            project_id = value.get("linear_project_id")
            if project_id is not None:
                project_id = _required_string(
                    project_id, f"Project {key} linear_project_id"
                )
            projects.append(
                ProjectBinding(
                    key=key,
                    name=_required_string(
                        value.get("name", key), f"Project {key} name"
                    ),
                    aliases=tuple(item.strip() for item in raw_aliases),
                    tracking_profile=tracking,
                    expected_project_id=project_id,
                )
            )

        return cls(
            connections=connections,
            tracking_connections=tracking_connections,
            projects=tuple(projects),
            source_path=source_path,
        )

    def verified_credential_profiles(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    connection.credential_profile
                    for connection in self.connections.values()
                    if connection.binding_state == "verified"
                }
            )
        )

    def resolve(
        self,
        *,
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> ResolvedRoute:
        if bool(project) == bool(connection_alias):
            raise AdapterError(
                "invalid_selector",
                "Provide exactly one of project or connection_alias.",
            )

        project_binding: ProjectBinding | None = None
        if project:
            normalized = _normalize(project)
            matches = [item for item in self.projects if normalized in item.selectors()]
            if not matches:
                raise AdapterError(
                    "unknown_project",
                    "No project matches the requested key or alias.",
                    details={"project": project},
                )
            if len(matches) != 1:
                raise AdapterError(
                    "ambiguous_project",
                    "The requested project selector is ambiguous.",
                    details={"project": project},
                )
            project_binding = matches[0]
            alias = self.tracking_connections[project_binding.tracking_profile]
        else:
            normalized = _normalize(connection_alias or "")
            matches = [
                alias for alias in self.connections if _normalize(alias) == normalized
            ]
            if not matches:
                raise AdapterError(
                    "unknown_connection",
                    "No Linear connection matches the requested alias.",
                    details={"connection_alias": connection_alias},
                )
            if len(matches) != 1:
                raise AdapterError(
                    "ambiguous_connection", "The Linear connection alias is ambiguous."
                )
            alias = matches[0]

        connection = self.connections[alias]
        if connection.binding_state != "verified":
            raise AdapterError(
                "connection_not_verified",
                "The selected Linear connection is not verified and cannot be used.",
                details={
                    "connection_alias": alias,
                    "binding_state": connection.binding_state,
                },
            )
        if not connection.expected_workspace_id:
            raise AdapterError(
                "invalid_manifest",
                "A verified connection requires an expected workspace ID.",
            )
        return ResolvedRoute(
            connection_alias=alias,
            credential_profile=connection.credential_profile,
            expected_workspace=connection.expected_workspace,
            expected_workspace_id=connection.expected_workspace_id,
            project_key=project_binding.key if project_binding else None,
            expected_project_id=(
                project_binding.expected_project_id if project_binding else None
            ),
        )


class SecretProvider(Protocol):
    def load(self, profile: str) -> str: ...


class OnePasswordSecretProvider:
    """Resolve a profile through an op:// template without shell interpolation."""

    def __init__(
        self,
        reference_template: str,
        *,
        executable: str = "op",
        timeout_seconds: float = 20.0,
    ) -> None:
        if "{profile}" not in reference_template:
            raise AdapterError(
                "invalid_configuration",
                "The 1Password reference template must contain {profile}.",
            )
        self.reference_template = reference_template
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def load(self, profile: str) -> str:
        encoded_profile = urllib.parse.quote(profile, safe="")
        reference = self.reference_template.format(profile=encoded_profile)
        try:
            completed = subprocess.run(
                [self.executable, "read", reference, "--no-newline"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError(
                "secret_provider_unavailable",
                "1Password CLI could not resolve the requested credential profile.",
                details={"credential_profile": profile},
            ) from exc
        if completed.returncode != 0:
            raise AdapterError(
                "secret_unavailable",
                "1Password did not return the requested credential profile.",
                details={"credential_profile": profile},
            )
        value = completed.stdout.strip()
        if not value:
            raise AdapterError(
                "secret_unavailable",
                "1Password returned an empty credential.",
                details={"credential_profile": profile},
            )
        return value


class MemoryCachingSecretProvider:
    """Load each secret once and retain it only in this process memory."""

    def __init__(self, backend: SecretProvider, redactor: SecretRedactor) -> None:
        self.backend = backend
        self.redactor = redactor
        self._cache: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, profile: str) -> str:
        with self._lock:
            cached = self._cache.get(profile)
            if cached is not None:
                return cached
            try:
                value = self.backend.load(profile)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(
                    "secret_unavailable",
                    "The credential provider failed without exposing runtime details.",
                    details={"credential_profile": profile},
                ) from exc
            if not isinstance(value, str) or not value:
                raise AdapterError(
                    "secret_unavailable",
                    "The credential provider returned an empty credential.",
                    details={"credential_profile": profile},
                )
            self.redactor.register(value)
            self._cache[profile] = value
            return value

    def preload(self, profiles: tuple[str, ...]) -> None:
        for profile in profiles:
            self.get(profile)


@dataclass(frozen=True)
class TeamIdentity:
    team_id: str
    key: str
    name: str


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    workspace_name: str
    teams: tuple[TeamIdentity, ...]


@dataclass(frozen=True)
class IssueRecord:
    issue_id: str
    identifier: str
    title: str
    description: str | None
    team_id: str
    project_id: str | None

    def public_payload(self) -> dict[str, Any]:
        return asdict(self)


class LinearTransport(Protocol):
    def discover_identity(self, token: str) -> WorkspaceIdentity: ...

    def get_issue(self, token: str, issue_id: str) -> IssueRecord: ...

    def create_issue(
        self,
        token: str,
        *,
        team_id: str,
        title: str,
        description: str | None,
        project_id: str | None,
    ) -> str: ...

    def update_issue(
        self,
        token: str,
        *,
        issue_id: str,
        title: str | None,
        description: str | None,
        description_is_set: bool,
    ) -> str: ...


class LinearGraphQLTransport:
    """Minimal Linear GraphQL transport with no response-body logging."""

    def __init__(
        self,
        *,
        endpoint: str = LINEAR_ENDPOINT,
        timeout_seconds: float = 20.0,
        auth_scheme: str = "api-key",
        redactor: SecretRedactor | None = None,
    ) -> None:
        if auth_scheme not in {"api-key", "bearer"}:
            raise AdapterError(
                "invalid_configuration", "auth_scheme must be api-key or bearer."
            )
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.auth_scheme = auth_scheme
        self.redactor = redactor or SecretRedactor()

    def _authorization(self, token: str) -> str:
        return f"Bearer {token}" if self.auth_scheme == "bearer" else token

    def _execute(
        self,
        token: str,
        query: str,
        variables: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.redactor.register(token)
        body = json.dumps({"query": query, "variables": dict(variables or {})}).encode()
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": self._authorization(token),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AdapterError(
                "linear_http_error",
                "Linear rejected the request.",
                details={"status": exc.code},
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AdapterError(
                "linear_transport_error",
                "Linear could not be reached or returned an invalid response.",
            ) from exc
        if status < 200 or status >= 300:
            raise AdapterError(
                "linear_http_error",
                "Linear returned a non-success status.",
                details={"status": status},
            )
        if not isinstance(payload, Mapping):
            raise AdapterError(
                "linear_protocol_error", "Linear returned an invalid payload."
            )
        errors = payload.get("errors")
        if errors:
            codes = sorted(
                {
                    str(error.get("extensions", {}).get("code"))
                    for error in errors
                    if isinstance(error, Mapping)
                    and isinstance(error.get("extensions"), Mapping)
                    and error.get("extensions", {}).get("code")
                }
            )
            raise AdapterError(
                "linear_graphql_error",
                "Linear reported a GraphQL error; response messages were suppressed.",
                details={"error_codes": codes},
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise AdapterError(
                "linear_protocol_error", "Linear returned no data mapping."
            )
        return data

    def discover_identity(self, token: str) -> WorkspaceIdentity:
        query = """
        query AdapterIdentity($after: String) {
          organization { id name }
          teams(first: 250, after: $after) {
            nodes { id key name }
            pageInfo { hasNextPage endCursor }
          }
        }
        """
        after: str | None = None
        organization: Mapping[str, Any] | None = None
        teams: list[TeamIdentity] = []
        while True:
            data = self._execute(token, query, {"after": after})
            raw_organization = data.get("organization")
            raw_teams = data.get("teams")
            if not isinstance(raw_organization, Mapping) or not isinstance(
                raw_teams, Mapping
            ):
                raise AdapterError(
                    "linear_protocol_error", "Linear identity response is incomplete."
                )
            if organization is None:
                organization = raw_organization
            elif organization.get("id") != raw_organization.get("id"):
                raise AdapterError(
                    "workspace_identity_mismatch",
                    "Workspace identity changed during discovery.",
                )
            nodes = raw_teams.get("nodes")
            if not isinstance(nodes, list):
                raise AdapterError(
                    "linear_protocol_error",
                    "Linear team identity response is incomplete.",
                )
            for item in nodes:
                if not isinstance(item, Mapping):
                    raise AdapterError(
                        "linear_protocol_error",
                        "Linear returned an invalid team identity.",
                    )
                teams.append(
                    TeamIdentity(
                        team_id=_required_string(item.get("id"), "Linear team id"),
                        key=_required_string(item.get("key"), "Linear team key"),
                        name=_required_string(item.get("name"), "Linear team name"),
                    )
                )
            page_info = raw_teams.get("pageInfo")
            if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
                break
            after = _required_string(page_info.get("endCursor"), "Linear team cursor")
        assert organization is not None
        return WorkspaceIdentity(
            workspace_id=_required_string(
                organization.get("id"), "Linear workspace id"
            ),
            workspace_name=_required_string(
                organization.get("name"), "Linear workspace name"
            ),
            teams=tuple(teams),
        )

    def get_issue(self, token: str, issue_id: str) -> IssueRecord:
        query = """
        query AdapterIssue($id: String!) {
          issue(id: $id) {
            id identifier title description
            team { id }
            project { id }
          }
        }
        """
        data = self._execute(token, query, {"id": issue_id})
        issue = data.get("issue")
        if not isinstance(issue, Mapping) or not isinstance(issue.get("team"), Mapping):
            raise AdapterError("issue_not_found", "The Linear issue could not be read.")
        project = issue.get("project")
        return IssueRecord(
            issue_id=_required_string(issue.get("id"), "Linear issue id"),
            identifier=_required_string(
                issue.get("identifier"), "Linear issue identifier"
            ),
            title=_required_string(issue.get("title"), "Linear issue title"),
            description=issue.get("description")
            if isinstance(issue.get("description"), str)
            else None,
            team_id=_required_string(issue["team"].get("id"), "Linear issue team id"),
            project_id=(
                _required_string(project.get("id"), "Linear issue project id")
                if isinstance(project, Mapping)
                else None
            ),
        )

    def create_issue(
        self,
        token: str,
        *,
        team_id: str,
        title: str,
        description: str | None,
        project_id: str | None,
    ) -> str:
        query = """
        mutation AdapterIssueCreate($input: IssueCreateInput!) {
          issueCreate(input: $input) { success issue { id } }
        }
        """
        input_value: dict[str, Any] = {"teamId": team_id, "title": title}
        if description is not None:
            input_value["description"] = description
        if project_id is not None:
            input_value["projectId"] = project_id
        data = self._execute(token, query, {"input": input_value})
        result = data.get("issueCreate")
        issue = result.get("issue") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("success") is not True
            or not isinstance(issue, Mapping)
        ):
            raise AdapterError(
                "mutation_failed", "Linear did not confirm issue creation."
            )
        return _required_string(issue.get("id"), "created Linear issue id")

    def update_issue(
        self,
        token: str,
        *,
        issue_id: str,
        title: str | None,
        description: str | None,
        description_is_set: bool,
    ) -> str:
        query = """
        mutation AdapterIssueUpdate($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) { success issue { id } }
        }
        """
        input_value: dict[str, Any] = {}
        if title is not None:
            input_value["title"] = title
        if description_is_set:
            input_value["description"] = description
        data = self._execute(token, query, {"id": issue_id, "input": input_value})
        result = data.get("issueUpdate")
        issue = result.get("issue") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or result.get("success") is not True
            or not isinstance(issue, Mapping)
        ):
            raise AdapterError(
                "mutation_failed", "Linear did not confirm the issue update."
            )
        return _required_string(issue.get("id"), "updated Linear issue id")


@dataclass(frozen=True)
class MutationPolicy:
    enabled: bool

    def authorize(self, operation: str, *, confirm: bool) -> None:
        if operation not in ALLOWED_MUTATIONS:
            raise AdapterError(
                "operation_not_allowed",
                "The requested mutation tool is not allowlisted.",
            )
        if not self.enabled:
            raise AdapterError(
                "mutations_disabled",
                "Mutation tools are disabled for this server process.",
            )
        if confirm is not True:
            raise AdapterError(
                "mutation_confirmation_required",
                "This mutation requires explicit per-call confirmation.",
            )


T = TypeVar("T")


class AdapterService:
    def __init__(
        self,
        *,
        manifest: Manifest,
        secrets: MemoryCachingSecretProvider,
        transport: LinearTransport,
        mutations_enabled: bool,
        redactor: SecretRedactor,
    ) -> None:
        self.manifest = manifest
        self.secrets = secrets
        self.transport = transport
        self.mutation_policy = MutationPolicy(enabled=mutations_enabled)
        self.redactor = redactor

    def _transport_call(
        self,
        action: Callable[[], T],
        *,
        error_code: str = "transport_error",
        message: str = "Linear transport failed without exposing runtime details.",
    ) -> T:
        try:
            return action()
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(error_code, message) from exc

    def _route_and_identity(
        self,
        *,
        project: str | None,
        connection_alias: str | None,
    ) -> tuple[ResolvedRoute, str, WorkspaceIdentity]:
        route = self.manifest.resolve(
            project=project, connection_alias=connection_alias
        )
        token = self.secrets.get(route.credential_profile)
        identity = self._transport_call(lambda: self.transport.discover_identity(token))
        if identity.workspace_id != route.expected_workspace_id:
            raise AdapterError(
                "workspace_identity_mismatch",
                "The credential resolved to a different Linear workspace.",
                details={"connection_alias": route.connection_alias},
            )
        return route, token, identity

    @staticmethod
    def _require_team(identity: WorkspaceIdentity, team_id: str) -> None:
        if team_id not in {team.team_id for team in identity.teams}:
            raise AdapterError(
                "team_identity_mismatch",
                "The requested team does not belong to the verified workspace identity.",
                details={"team_id": team_id},
            )

    @staticmethod
    def _verify_issue_identity(
        route: ResolvedRoute,
        identity: WorkspaceIdentity,
        issue: IssueRecord,
        *,
        expected_team_id: str | None = None,
        expected_project_id: str | None = None,
        error_code: str = "identity_mismatch",
    ) -> None:
        team_ids = {team.team_id for team in identity.teams}
        if issue.team_id not in team_ids or (
            expected_team_id is not None and issue.team_id != expected_team_id
        ):
            raise AdapterError(
                error_code, "Linear issue team identity verification failed."
            )
        project_id = expected_project_id or route.expected_project_id
        if project_id is not None and issue.project_id != project_id:
            raise AdapterError(
                error_code, "Linear issue project identity verification failed."
            )

    def resolve_route(
        self,
        *,
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        return {
            "route": self.manifest.resolve(
                project=project, connection_alias=connection_alias
            ).public_payload()
        }

    def discover(
        self,
        *,
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        route, _, identity = self._route_and_identity(
            project=project, connection_alias=connection_alias
        )
        return {
            "route": route.public_payload(),
            "identity": {
                "workspace_id": identity.workspace_id,
                "workspace_name": identity.workspace_name,
                "teams": [asdict(team) for team in identity.teams],
            },
            "verification": "workspace-identity-verified",
        }

    def get_issue(
        self,
        *,
        issue_id: str,
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        route, token, identity = self._route_and_identity(
            project=project, connection_alias=connection_alias
        )
        issue = self._transport_call(lambda: self.transport.get_issue(token, issue_id))
        self._verify_issue_identity(route, identity, issue)
        return {
            "route": route.public_payload(),
            "issue": issue.public_payload(),
            "verification": "workspace-team-project-verified",
        }

    def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        project: str | None = None,
        connection_alias: str | None = None,
        description: str | None = None,
        project_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self.mutation_policy.authorize("issue_create", confirm=confirm)
        if not isinstance(title, str) or not title.strip():
            raise AdapterError("invalid_request", "Issue title must be non-empty.")
        route, token, identity = self._route_and_identity(
            project=project, connection_alias=connection_alias
        )
        self._require_team(identity, team_id)
        if (
            route.expected_project_id
            and project_id
            and project_id != route.expected_project_id
        ):
            raise AdapterError(
                "project_identity_mismatch",
                "The requested project does not match the manifest project binding.",
            )
        effective_project_id = route.expected_project_id or project_id
        issue_id = self._transport_call(
            lambda: self.transport.create_issue(
                token,
                team_id=team_id,
                title=title.strip(),
                description=description,
                project_id=effective_project_id,
            ),
            error_code="mutation_failed",
            message="Linear issue creation failed without exposing payload details.",
        )
        try:
            issue = self._transport_call(
                lambda: self.transport.get_issue(token, issue_id),
                error_code="read_back_failed",
                message="The created issue could not be read back.",
            )
            self._verify_issue_identity(
                route,
                identity,
                issue,
                expected_team_id=team_id,
                expected_project_id=effective_project_id,
                error_code="read_back_failed",
            )
            if issue.title != title.strip() or issue.description != description:
                raise AdapterError(
                    "read_back_failed",
                    "Created issue fields did not match the mutation request.",
                )
        except AdapterError as exc:
            raise AdapterError(
                "read_back_failed",
                "The create mutation completed but read-back verification failed.",
                details={
                    "issue_id": issue_id,
                    "mutation_may_have_succeeded": True,
                },
            ) from exc
        return {
            "route": route.public_payload(),
            "issue": issue.public_payload(),
            "verification": "read-back-verified",
        }

    def update_issue(
        self,
        *,
        issue_id: str,
        project: str | None = None,
        connection_alias: str | None = None,
        title: str | None = None,
        description: str | None = None,
        clear_description: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        self.mutation_policy.authorize("issue_update", confirm=confirm)
        if title is not None and not title.strip():
            raise AdapterError(
                "invalid_request", "Issue title must be non-empty when set."
            )
        if clear_description and description is not None:
            raise AdapterError(
                "invalid_request",
                "Use either description or clear_description, not both.",
            )
        description_is_set = description is not None or clear_description
        if title is None and not description_is_set:
            raise AdapterError(
                "invalid_request", "No supported update fields were supplied."
            )
        route, token, identity = self._route_and_identity(
            project=project, connection_alias=connection_alias
        )
        current = self._transport_call(
            lambda: self.transport.get_issue(token, issue_id)
        )
        self._verify_issue_identity(route, identity, current)
        normalized_title = title.strip() if title is not None else None
        target_description = None if clear_description else description
        updated_id = self._transport_call(
            lambda: self.transport.update_issue(
                token,
                issue_id=current.issue_id,
                title=normalized_title,
                description=target_description,
                description_is_set=description_is_set,
            ),
            error_code="mutation_failed",
            message="Linear issue update failed without exposing payload details.",
        )
        try:
            issue = self._transport_call(
                lambda: self.transport.get_issue(token, updated_id),
                error_code="read_back_failed",
                message="The updated issue could not be read back.",
            )
            self._verify_issue_identity(
                route,
                identity,
                issue,
                expected_team_id=current.team_id,
                error_code="read_back_failed",
            )
            if normalized_title is not None and issue.title != normalized_title:
                raise AdapterError(
                    "read_back_failed", "Updated issue title did not match."
                )
            if description_is_set and issue.description != target_description:
                raise AdapterError(
                    "read_back_failed", "Updated issue description did not match."
                )
        except AdapterError as exc:
            raise AdapterError(
                "read_back_failed",
                "The update mutation completed but read-back verification failed.",
                details={
                    "issue_id": updated_id,
                    "mutation_may_have_succeeded": True,
                },
            ) from exc
        return {
            "route": route.public_payload(),
            "issue": issue.public_payload(),
            "verification": "read-back-verified",
        }


def build_mcp(service: AdapterService) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - inline uv metadata supplies it
        raise AdapterError(
            "dependency_missing",
            "The MCP Python SDK is required; run the script through uv.",
        ) from exc

    server = FastMCP(
        "Dual Linear MCP",
        instructions=(
            "Resolve exactly one manifest route. Use discovery before writes. "
            "Mutation tools require server enablement and confirm=true."
        ),
        json_response=True,
    )

    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    writes = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )

    @server.tool(annotations=read_only)
    def resolve_linear_route(
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one verified manifest route without loading a credential."""

        return safe_tool_call(
            service.redactor,
            lambda: service.resolve_route(
                project=project, connection_alias=connection_alias
            ),
        )

    @server.tool(annotations=read_only)
    def linear_discover(
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        """Verify the selected workspace and list teams using one credential."""

        return safe_tool_call(
            service.redactor,
            lambda: service.discover(
                project=project, connection_alias=connection_alias
            ),
        )

    @server.tool(annotations=read_only)
    def linear_get_issue(
        issue_id: str,
        project: str | None = None,
        connection_alias: str | None = None,
    ) -> dict[str, Any]:
        """Read an issue only after workspace, team, and project checks."""

        return safe_tool_call(
            service.redactor,
            lambda: service.get_issue(
                issue_id=issue_id, project=project, connection_alias=connection_alias
            ),
        )

    @server.tool(annotations=writes)
    def linear_create_issue(
        team_id: str,
        title: str,
        project: str | None = None,
        connection_alias: str | None = None,
        description: str | None = None,
        project_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Create and read back an issue through the gated mutation surface."""

        return safe_tool_call(
            service.redactor,
            lambda: service.create_issue(
                project=project,
                connection_alias=connection_alias,
                team_id=team_id,
                title=title,
                description=description,
                project_id=project_id,
                confirm=confirm,
            ),
        )

    @server.tool(annotations=writes)
    def linear_update_issue(
        issue_id: str,
        project: str | None = None,
        connection_alias: str | None = None,
        title: str | None = None,
        description: str | None = None,
        clear_description: bool = False,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Update supported issue fields and require a verified read-back."""

        return safe_tool_call(
            service.redactor,
            lambda: service.update_issue(
                issue_id=issue_id,
                project=project,
                connection_alias=connection_alias,
                title=title,
                description=description,
                clear_description=clear_description,
                confirm=confirm,
            ),
        )

    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--manifest", type=Path, required=True)
    selector = resolve.add_mutually_exclusive_group(required=True)
    selector.add_argument("--project")
    selector.add_argument("--connection-alias")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument(
        "--op-reference-template",
        default=os.environ.get("DUAL_LINEAR_OP_REFERENCE_TEMPLATE"),
        help="Non-secret op:// URI template containing {profile}.",
    )
    serve.add_argument("--op-executable", default="op")
    serve.add_argument("--linear-endpoint", default=LINEAR_ENDPOINT)
    serve.add_argument(
        "--auth-scheme", choices=("api-key", "bearer"), default="api-key"
    )
    serve.add_argument("--timeout-seconds", type=float, default=20.0)
    serve.add_argument("--enable-mutations", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    redactor = SecretRedactor()
    try:
        manifest = Manifest.load(args.manifest)
        if args.command == "validate-manifest":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "connections": len(manifest.connections),
                        "projects": len(manifest.projects),
                        "verified_profiles": len(
                            manifest.verified_credential_profiles()
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "resolve":
            route = manifest.resolve(
                project=args.project, connection_alias=args.connection_alias
            )
            print(
                json.dumps(
                    {"ok": True, "route": route.public_payload()}, sort_keys=True
                )
            )
            return 0
        if not args.op_reference_template:
            raise AdapterError(
                "invalid_configuration",
                "serve requires --op-reference-template or DUAL_LINEAR_OP_REFERENCE_TEMPLATE.",
            )
        backend = OnePasswordSecretProvider(
            args.op_reference_template,
            executable=args.op_executable,
            timeout_seconds=args.timeout_seconds,
        )
        secrets = MemoryCachingSecretProvider(backend, redactor)
        secrets.preload(manifest.verified_credential_profiles())
        transport = LinearGraphQLTransport(
            endpoint=args.linear_endpoint,
            timeout_seconds=args.timeout_seconds,
            auth_scheme=args.auth_scheme,
            redactor=redactor,
        )
        service = AdapterService(
            manifest=manifest,
            secrets=secrets,
            transport=transport,
            mutations_enabled=args.enable_mutations,
            redactor=redactor,
        )
        build_mcp(service).run(transport="stdio")
        return 0
    except AdapterError as exc:
        print(
            json.dumps(
                {"ok": False, "error": exc.safe_payload(redactor)}, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
