from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from mcp import types


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "official-linear-mcp-bridge"
    / "scripts"
    / "official_linear_mcp_bridge.py"
)

spec = importlib.util.spec_from_file_location("official_linear_mcp_bridge", SCRIPT)
assert spec is not None and spec.loader is not None
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class FakeSession:
    def __init__(
        self,
        list_result: types.ListToolsResult,
        call_result: types.CallToolResult,
    ) -> None:
        self.list_result = list_result
        self.call_result = call_result
        self.requests: list[tuple[object, type[object]]] = []

    async def send_request(
        self,
        request: object,
        result_type: type[object],
        **_kwargs: object,
    ) -> object:
        self.requests.append((request, result_type))
        if result_type is types.ListToolsResult:
            return self.list_result
        if result_type is types.CallToolResult:
            return self.call_result
        raise AssertionError("unexpected result type")


class ForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = types.Tool(
            name="official_tool",
            title="Official tool",
            description="Upstream-owned description",
            inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
            outputSchema={"type": "object"},
            annotations=types.ToolAnnotations(
                title="Official annotation",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=True,
            ),
            _meta={"upstream": "preserved"},
        )
        self.list_result = types.ListToolsResult(
            tools=[self.tool],
            nextCursor="official-next",
            _meta={"catalog": "preserved"},
        )
        self.call_result = types.CallToolResult(
            content=[types.TextContent(type="text", text="official result")],
            structuredContent={"exact": True},
            isError=False,
            _meta={"result": "preserved"},
        )
        self.session = FakeSession(self.list_result, self.call_result)
        self.forwarder = bridge.OfficialLinearForwarder(self.session)

    def test_list_request_and_exact_catalog_are_forwarded(self) -> None:
        params = types.PaginatedRequestParams(
            cursor="official-cursor", _meta={"request": "preserved"}
        )
        request = types.ListToolsRequest.model_validate(
            {
                "jsonrpc": "2.0",
                "id": 41,
                "method": "tools/list",
                "params": params,
            }
        )

        result = asyncio.run(self.forwarder.list_tools(request))

        self.assertIs(result, self.list_result)
        forwarded, result_type = self.session.requests[0]
        original_dump = request.model_dump(by_alias=True, exclude_none=True)
        forwarded_dump = forwarded.root.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(original_dump["id"], 41)
        self.assertEqual(original_dump["jsonrpc"], "2.0")
        self.assertEqual(set(forwarded_dump), {"method", "params"})
        self.assertIs(forwarded.root.params, request.params)
        self.assertEqual(forwarded_dump["params"], original_dump["params"])
        self.assertIs(result_type, types.ListToolsResult)
        self.assertIs(result.tools[0], self.tool)
        self.assertEqual(result.tools[0].annotations.destructiveHint, True)
        self.assertEqual(result.tools[0].meta, {"upstream": "preserved"})

    def test_call_request_and_exact_result_are_forwarded(self) -> None:
        params = types.CallToolRequestParams.model_validate(
            {
                "name": "official_tool",
                "arguments": {"value": "unchanged"},
                "_meta": {"request": "preserved"},
                "futureParameter": "preserved",
            }
        )
        request = types.CallToolRequest.model_validate(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": params,
            }
        )

        result = asyncio.run(self.forwarder.call_tool(request))

        self.assertIs(result, self.call_result)
        forwarded, result_type = self.session.requests[0]
        original_dump = request.model_dump(by_alias=True, exclude_none=True)
        forwarded_dump = forwarded.root.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(original_dump["id"], 42)
        self.assertEqual(original_dump["jsonrpc"], "2.0")
        self.assertEqual(set(forwarded_dump), {"method", "params"})
        self.assertIs(forwarded.root.params, request.params)
        self.assertEqual(forwarded_dump["params"], original_dump["params"])
        self.assertEqual(forwarded_dump["params"]["futureParameter"], "preserved")
        self.assertIs(result_type, types.CallToolResult)

    def test_server_handlers_return_upstream_results_without_local_tools(self) -> None:
        server = bridge.build_server(self.forwarder)
        list_request = types.ListToolsRequest()
        call_request = types.CallToolRequest(
            params=types.CallToolRequestParams(name="official_tool", arguments={})
        )

        listed = asyncio.run(
            server.request_handlers[types.ListToolsRequest](list_request)
        )
        called = asyncio.run(
            server.request_handlers[types.CallToolRequest](call_request)
        )

        self.assertIs(listed.root, self.list_result)
        self.assertIs(called.root, self.call_result)
        self.assertEqual(
            set(server.request_handlers),
            {types.PingRequest, types.ListToolsRequest, types.CallToolRequest},
        )

    def test_upstream_failures_are_sanitized_and_never_retried(self) -> None:
        secret = "linear-secret-never-print"

        class FailingSession:
            def __init__(self) -> None:
                self.calls = 0

            async def send_request(self, *_args: object, **_kwargs: object) -> object:
                self.calls += 1
                raise RuntimeError(f"upstream leaked {secret}")

        session = FailingSession()
        forwarder = bridge.OfficialLinearForwarder(session)

        with self.assertRaises(bridge.BridgeError) as caught:
            asyncio.run(forwarder.list_tools(types.ListToolsRequest()))

        self.assertEqual(session.calls, 1)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(caught.exception.code, "upstream_failed")


class CredentialTests(unittest.TestCase):
    def test_provider_resolves_once_with_exact_account_and_scrubbed_environment(
        self,
    ) -> None:
        secret = "linear-runtime-secret"
        completed = subprocess.CompletedProcess([], 0, secret + "\n", "")
        provider = bridge.OnePasswordBearerProvider(
            "op://Example/linear-a/credential",
            account="example.1password.com",
        )
        inherited = {
            "PATH": "/usr/bin",
            "SAFE_VALUE": "preserved",
            "OP_ACCOUNT": "ambient-account",
            "OP_SESSION": "ambient-session",
            "OP_SESSION_work": "ambient-session-two",
            "OP_SERVICE_ACCOUNT_TOKEN": "ambient-service",
            "OP_CONNECT_HOST": "https://connect.invalid",
            "OP_CONNECT_TOKEN": "ambient-connect",
        }

        with (
            mock.patch.dict(os.environ, inherited, clear=True),
            mock.patch.object(bridge.subprocess, "run", return_value=completed) as run,
        ):
            first = provider.resolve()
            second = provider.resolve()

        self.assertEqual(first, secret)
        self.assertEqual(second, secret)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0],
            ["op", "inject", "--account", "example.1password.com"],
        )
        self.assertEqual(
            run.call_args.kwargs["input"],
            "{{ op://Example/linear-a/credential }}",
        )
        self.assertEqual(
            run.call_args.kwargs["env"],
            {"PATH": "/usr/bin", "SAFE_VALUE": "preserved"},
        )
        self.assertNotIn(secret, run.call_args.args[0])
        self.assertNotIn(secret, run.call_args.kwargs["input"])

    def test_provider_failure_suppresses_stdout_stderr_and_has_no_fallback(
        self,
    ) -> None:
        secret = "private-provider-output"
        completed = subprocess.CompletedProcess([], 1, secret, secret)
        provider = bridge.OnePasswordBearerProvider(
            "op://Example/linear-b/credential",
            account="example.1password.com",
        )

        with mock.patch.object(bridge.subprocess, "run", return_value=completed) as run:
            for _ in range(2):
                with self.assertRaises(bridge.BridgeError) as caught:
                    provider.resolve()
                self.assertNotIn(secret, str(caught.exception))

        self.assertEqual(run.call_count, 1)

    def test_cli_and_endpoint_are_pinned_without_oauth_or_endpoint_override(
        self,
    ) -> None:
        self.assertEqual(bridge.LINEAR_MCP_ENDPOINT, "https://mcp.linear.app/mcp")
        parser = bridge._parser()
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            parser.parse_args(
                [
                    "serve",
                    "--op-reference",
                    "op://Example/linear-a/credential",
                    "--op-account",
                    "example.1password.com",
                    "--endpoint",
                    "https://example.invalid",
                ]
            )
        source = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("oauth", source)
        self.assertNotIn("graphql", source)
        self.assertNotIn("browser", source)
        self.assertNotIn("retry", source)

    def test_reference_and_account_syntax_fail_closed(self) -> None:
        with self.assertRaises(bridge.BridgeError):
            bridge.OnePasswordBearerProvider(
                "https://example.invalid/item/credential",
                account="example.1password.com",
            )
        with self.assertRaises(bridge.BridgeError):
            bridge.OnePasswordBearerProvider(
                "op://Example/linear-a/credential", account="not an account"
            )
        with self.assertRaises(bridge.BridgeError):
            bridge.OnePasswordBearerProvider(
                "op://Example/too/many/path/segments/credential",
                account="example.1password.com",
            )

    def test_main_suppresses_runtime_exception_details(self) -> None:
        secret = "runtime-exception-secret"
        with (
            mock.patch.object(
                bridge.OnePasswordBearerProvider, "resolve", return_value="safe-fake"
            ),
            mock.patch.object(bridge, "_serve", side_effect=RuntimeError(secret)),
            mock.patch("builtins.print") as output,
        ):
            result = bridge.main(
                [
                    "serve",
                    "--op-reference",
                    "op://Example/linear-a/credential",
                    "--op-account",
                    "example.1password.com",
                ]
            )

        self.assertEqual(result, 2)
        self.assertNotIn(secret, output.call_args.args[0])
        self.assertIn("failed closed", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
