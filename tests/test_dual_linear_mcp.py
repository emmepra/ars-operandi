from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "dual-linear-mcp" / "scripts" / "dual_linear_mcp.py"
FIXTURE = ROOT / "skills" / "dual-linear-mcp" / "references" / "example-manifest.yaml"

spec = importlib.util.spec_from_file_location("dual_linear_mcp", SCRIPT)
assert spec is not None and spec.loader is not None
dual_linear_mcp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dual_linear_mcp
spec.loader.exec_module(dual_linear_mcp)


class CountingSecretProvider:
    def __init__(self, values: dict[str, str]):
        self.values = values
        self.calls: list[str] = []

    def load(self, profile: str) -> str:
        self.calls.append(profile)
        return self.values[profile]


class FakeLinearTransport:
    def __init__(self) -> None:
        self.identities = {
            "token-alpha": dual_linear_mcp.WorkspaceIdentity(
                workspace_id="workspace-alpha",
                workspace_name="Sample Alpha",
                teams=(
                    dual_linear_mcp.TeamIdentity(
                        team_id="team-alpha", key="ALPHA", name="Alpha Team"
                    ),
                ),
            ),
            "token-beta": dual_linear_mcp.WorkspaceIdentity(
                workspace_id="workspace-beta",
                workspace_name="Sample Beta",
                teams=(
                    dual_linear_mcp.TeamIdentity(
                        team_id="team-beta", key="BETA", name="Beta Team"
                    ),
                ),
            ),
        }
        self.issues: dict[str, dual_linear_mcp.IssueRecord] = {}
        self.readback_override: dual_linear_mcp.IssueRecord | None = None
        self.raise_with_token = False
        self.created_with: list[str] = []

    def discover_identity(self, token: str) -> dual_linear_mcp.WorkspaceIdentity:
        if self.raise_with_token:
            raise RuntimeError(f"transport rejected credential {token}")
        return self.identities[token]

    def get_issue(self, token: str, issue_id: str) -> dual_linear_mcp.IssueRecord:
        if self.readback_override is not None:
            return self.readback_override
        return self.issues[issue_id]

    def create_issue(
        self,
        token: str,
        *,
        team_id: str,
        title: str,
        description: str | None,
        project_id: str | None,
    ) -> str:
        self.created_with.append(token)
        issue_id = f"issue-{len(self.issues) + 1}"
        self.issues[issue_id] = dual_linear_mcp.IssueRecord(
            issue_id=issue_id,
            identifier=f"ALPHA-{len(self.issues) + 1}",
            title=title,
            description=description,
            team_id=team_id,
            project_id=project_id,
        )
        return issue_id

    def update_issue(
        self,
        token: str,
        *,
        issue_id: str,
        title: str | None,
        description: str | None,
        description_is_set: bool,
    ) -> str:
        current = self.issues[issue_id]
        self.issues[issue_id] = dual_linear_mcp.IssueRecord(
            issue_id=current.issue_id,
            identifier=current.identifier,
            title=title if title is not None else current.title,
            description=description if description_is_set else current.description,
            team_id=current.team_id,
            project_id=current.project_id,
        )
        return issue_id


def build_service(
    *,
    tokens: dict[str, str] | None = None,
    mutations_enabled: bool = False,
) -> tuple[object, CountingSecretProvider, FakeLinearTransport]:
    manifest = dual_linear_mcp.Manifest.load(FIXTURE)
    backend = CountingSecretProvider(
        tokens
        or {
            "credential-alpha": "token-alpha",
            "credential-beta": "token-beta",
        }
    )
    redactor = dual_linear_mcp.SecretRedactor()
    secrets = dual_linear_mcp.MemoryCachingSecretProvider(backend, redactor)
    transport = FakeLinearTransport()
    service = dual_linear_mcp.AdapterService(
        manifest=manifest,
        secrets=secrets,
        transport=transport,
        mutations_enabled=mutations_enabled,
        redactor=redactor,
    )
    return service, backend, transport


class ManifestAndRoutingTests(unittest.TestCase):
    def test_two_profiles_resolve_by_project_and_explicit_alias(self) -> None:
        manifest = dual_linear_mcp.Manifest.load(FIXTURE)

        alpha = manifest.resolve(project="alpha")
        beta = manifest.resolve(connection_alias="linear-beta")

        self.assertEqual(alpha.connection_alias, "linear-alpha")
        self.assertEqual(alpha.expected_project_id, "project-alpha")
        self.assertEqual(beta.connection_alias, "linear-beta")

    def test_unknown_project_fails_closed(self) -> None:
        manifest = dual_linear_mcp.Manifest.load(FIXTURE)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            manifest.resolve(project="does-not-exist")

        self.assertEqual(caught.exception.code, "unknown_project")

    def test_unknown_explicit_alias_fails_closed(self) -> None:
        manifest = dual_linear_mcp.Manifest.load(FIXTURE)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            manifest.resolve(connection_alias="linear-unknown")

        self.assertEqual(caught.exception.code, "unknown_connection")

    def test_ambiguous_project_alias_fails_closed(self) -> None:
        data = dual_linear_mcp.load_yaml(FIXTURE)
        data["projects"][1]["aliases"].append("alpha")
        manifest = dual_linear_mcp.Manifest.from_data(data)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            manifest.resolve(project="alpha")

        self.assertEqual(caught.exception.code, "ambiguous_project")

    def test_planned_binding_fails_closed(self) -> None:
        data = dual_linear_mcp.load_yaml(FIXTURE)
        data["linear_connections"]["linear-alpha"]["binding_state"] = "planned"
        data["linear_connections"]["linear-alpha"].pop("expected_workspace_id")
        manifest = dual_linear_mcp.Manifest.from_data(data)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            manifest.resolve(project="sample-alpha")

        self.assertEqual(caught.exception.code, "connection_not_verified")

    def test_missing_connection_reference_is_rejected(self) -> None:
        data = dual_linear_mcp.load_yaml(FIXTURE)
        data["tracking_profiles"]["alpha"]["portfolio"]["connection"] = "missing"

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            dual_linear_mcp.Manifest.from_data(data)

        self.assertEqual(caught.exception.code, "invalid_manifest")

    def test_unknown_adapter_is_rejected(self) -> None:
        data = dual_linear_mcp.load_yaml(FIXTURE)
        data["linear_connections"]["linear-alpha"]["adapter"] = "other-adapter"

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            dual_linear_mcp.Manifest.from_data(data)

        self.assertEqual(caught.exception.code, "invalid_manifest")


class SecretAndIdentityTests(unittest.TestCase):
    def test_secret_is_loaded_once_per_process_cache(self) -> None:
        service, backend, _ = build_service()

        service.discover(project="sample-alpha")
        service.discover(project="sample-alpha")

        self.assertEqual(backend.calls, ["credential-alpha"])

    def test_preload_loads_both_verified_profiles_once(self) -> None:
        service, backend, _ = build_service()

        service.secrets.preload(service.manifest.verified_credential_profiles())
        service.discover(project="sample-alpha")
        service.discover(project="sample-beta")

        self.assertEqual(sorted(backend.calls), ["credential-alpha", "credential-beta"])

    def test_onepassword_provider_uses_op_read_without_shell(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="runtime-secret", stderr=""
        )
        provider = dual_linear_mcp.OnePasswordSecretProvider(
            "op://Sample/{profile}/api-key"
        )

        with mock.patch.object(
            dual_linear_mcp.subprocess, "run", return_value=completed
        ) as run:
            value = provider.load("credential alpha")

        self.assertEqual(value, "runtime-secret")
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            ["op", "read", "op://Sample/credential%20alpha/api-key", "--no-newline"],
        )
        self.assertNotIn("runtime-secret", command)

    def test_onepassword_failure_suppresses_cli_stderr(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="runtime-secret"
        )
        provider = dual_linear_mcp.OnePasswordSecretProvider(
            "op://Sample/{profile}/api-key"
        )

        with mock.patch.object(
            dual_linear_mcp.subprocess, "run", return_value=completed
        ):
            with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
                provider.load("credential-alpha")

        self.assertNotIn("runtime-secret", str(caught.exception))

    def test_swapped_credentials_are_rejected_before_mutation(self) -> None:
        service, _, transport = build_service(
            tokens={
                "credential-alpha": "token-beta",
                "credential-beta": "token-alpha",
            },
            mutations_enabled=True,
        )

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-alpha",
                title="Safe sample",
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "workspace_identity_mismatch")
        self.assertEqual(transport.created_with, [])

    def test_unknown_team_is_rejected_before_mutation(self) -> None:
        service, _, transport = build_service(mutations_enabled=True)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-unknown",
                title="Safe sample",
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "team_identity_mismatch")
        self.assertEqual(transport.created_with, [])

    def test_secret_is_redacted_from_safe_tool_error(self) -> None:
        service, _, transport = build_service()
        transport.raise_with_token = True

        result = dual_linear_mcp.safe_tool_call(
            service.redactor,
            lambda: service.discover(project="sample-alpha"),
        )

        dumped = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertNotIn("token-alpha", dumped)
        self.assertEqual(result["error"]["code"], "transport_error")


class MutationGuardTests(unittest.TestCase):
    def test_mutation_requires_server_gate(self) -> None:
        service, _, _ = build_service(mutations_enabled=False)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-alpha",
                title="Safe sample",
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "mutations_disabled")

    def test_mutation_requires_per_call_confirmation(self) -> None:
        service, _, _ = build_service(mutations_enabled=True)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-alpha",
                title="Safe sample",
                confirm=False,
            )

        self.assertEqual(caught.exception.code, "mutation_confirmation_required")

    def test_unsupported_operation_is_never_allowed(self) -> None:
        service, _, _ = build_service(mutations_enabled=True)

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.mutation_policy.authorize("issue_delete", confirm=True)

        self.assertEqual(caught.exception.code, "operation_not_allowed")

    def test_create_issue_is_read_back_and_verified(self) -> None:
        service, _, _ = build_service(mutations_enabled=True)

        result = service.create_issue(
            project="sample-alpha",
            team_id="team-alpha",
            title="Safe sample",
            description="Generic description",
            confirm=True,
        )

        self.assertEqual(result["issue"]["title"], "Safe sample")
        self.assertEqual(result["verification"], "read-back-verified")

    def test_read_back_mismatch_fails_closed(self) -> None:
        service, _, transport = build_service(mutations_enabled=True)
        transport.readback_override = dual_linear_mcp.IssueRecord(
            issue_id="issue-1",
            identifier="ALPHA-1",
            title="Different title",
            description="Generic description",
            team_id="team-alpha",
            project_id="project-alpha",
        )

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-alpha",
                title="Safe sample",
                description="Generic description",
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "read_back_failed")
        self.assertEqual(caught.exception.details["issue_id"], "issue-1")
        self.assertTrue(caught.exception.details["mutation_may_have_succeeded"])

    def test_read_back_project_mismatch_fails_closed(self) -> None:
        service, _, transport = build_service(mutations_enabled=True)
        transport.readback_override = dual_linear_mcp.IssueRecord(
            issue_id="issue-1",
            identifier="ALPHA-1",
            title="Safe sample",
            description=None,
            team_id="team-alpha",
            project_id="project-other",
        )

        with self.assertRaises(dual_linear_mcp.AdapterError) as caught:
            service.create_issue(
                project="sample-alpha",
                team_id="team-alpha",
                title="Safe sample",
                confirm=True,
            )

        self.assertEqual(caught.exception.code, "read_back_failed")

    def test_update_issue_is_preflighted_and_read_back(self) -> None:
        service, _, transport = build_service(mutations_enabled=True)
        transport.issues["issue-1"] = dual_linear_mcp.IssueRecord(
            issue_id="issue-1",
            identifier="ALPHA-1",
            title="Before",
            description="Before description",
            team_id="team-alpha",
            project_id="project-alpha",
        )

        result = service.update_issue(
            project="sample-alpha",
            issue_id="issue-1",
            title="After",
            confirm=True,
        )

        self.assertEqual(result["issue"]["title"], "After")
        self.assertEqual(result["verification"], "read-back-verified")


class McpContractTests(unittest.TestCase):
    def test_mcp_exposes_only_the_allowlisted_tools_with_annotations(self) -> None:
        service, _, _ = build_service()
        server = dual_linear_mcp.build_mcp(service)

        tools = asyncio.run(server.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(set(by_name), set(dual_linear_mcp.TOOL_NAMES))
        self.assertTrue(by_name["linear_discover"].annotations.readOnlyHint)
        self.assertFalse(by_name["linear_create_issue"].annotations.readOnlyHint)


if __name__ == "__main__":
    unittest.main()
