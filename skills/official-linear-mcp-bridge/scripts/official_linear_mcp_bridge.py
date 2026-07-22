#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==1.28.1"]
# ///
"""Expose one official Linear MCP connection over local STDIO."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from typing import Any

import httpx
from mcp import ClientSession, McpError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


LINEAR_MCP_ENDPOINT = "https://mcp.linear.app/mcp"
_ACCOUNT_PATTERN = re.compile(
    r"(?=.{3,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_REFERENCE_SEGMENT_PATTERN = re.compile(
    r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2}){1,128}\Z"
)
ONEPASSWORD_AUTH_ENV_KEYS = frozenset(
    {
        "OP_ACCOUNT",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_SESSION",
    }
)


class BridgeError(RuntimeError):
    """A fail-closed error whose message is safe to expose."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _clean_onepassword_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ONEPASSWORD_AUTH_ENV_KEYS and not key.startswith("OP_SESSION")
    }


def _valid_account(value: str) -> bool:
    return bool(_ACCOUNT_PATTERN.fullmatch(value))


def _valid_reference(value: str) -> bool:
    if len(value) > 512 or not value.startswith("op://"):
        return False
    segments = value[5:].split("/")
    return len(segments) in {3, 4} and all(
        _REFERENCE_SEGMENT_PATTERN.fullmatch(segment) for segment in segments
    )


class OnePasswordBearerProvider:
    """Resolve one approved reference once and retain it only in process memory."""

    def __init__(
        self,
        reference: str,
        *,
        account: str,
        executable: str = "op",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not _valid_reference(reference):
            raise BridgeError(
                "invalid_reference",
                "The 1Password reference is not a valid static secret reference.",
            )
        if not _valid_account(account):
            raise BridgeError(
                "invalid_account",
                "The explicit 1Password account selector is invalid.",
            )
        if timeout_seconds <= 0:
            raise BridgeError(
                "invalid_configuration", "The provider timeout must be positive."
            )
        self.reference = reference
        self.account = account
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self._resolved: str | None = None
        self._failure: BridgeError | None = None
        self._attempted = False

    def resolve(self) -> str:
        if self._resolved is not None:
            return self._resolved
        if self._failure is not None:
            raise self._failure
        if self._attempted:
            raise BridgeError(
                "credential_unavailable",
                "The Linear credential is unavailable; restart the MCP server.",
            )
        self._attempted = True
        try:
            completed = subprocess.run(
                [self.executable, "inject", "--account", self.account],
                input=f"{{{{ {self.reference} }}}}",
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout_seconds,
                env=_clean_onepassword_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._failure = BridgeError(
                "credential_unavailable",
                "Unlock the 1Password desktop app, then restart this MCP server.",
            )
            raise self._failure from exc
        value = completed.stdout.strip()
        if completed.returncode != 0 or not value or "\n" in value or "\r" in value:
            self._failure = BridgeError(
                "credential_unavailable",
                "Unlock the 1Password desktop app, then restart this MCP server.",
            )
            raise self._failure
        self._resolved = value
        return value


class OfficialLinearForwarder:
    """Forward protocol requests and upstream result models without adaptation."""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def list_tools(
        self, request: types.ListToolsRequest
    ) -> types.ListToolsResult:
        try:
            upstream_request = types.ListToolsRequest(params=request.params)
            return await self.session.send_request(
                types.ClientRequest(root=upstream_request), types.ListToolsResult
            )
        except Exception as exc:
            raise BridgeError(
                "upstream_failed",
                "The official Linear MCP catalog request failed closed.",
            ) from exc

    async def call_tool(self, request: types.CallToolRequest) -> types.CallToolResult:
        try:
            upstream_request = types.CallToolRequest(params=request.params)
            return await self.session.send_request(
                types.ClientRequest(root=upstream_request), types.CallToolResult
            )
        except Exception as exc:
            raise BridgeError(
                "upstream_failed",
                "The official Linear MCP tool request failed closed.",
            ) from exc


def _protocol_error(error: BridgeError) -> McpError:
    return McpError(types.ErrorData(code=types.INTERNAL_ERROR, message=error.message))


def build_server(forwarder: OfficialLinearForwarder) -> Server[Any]:
    server: Server[Any] = Server(
        "official-linear-mcp-bridge",
        version="1",
        instructions="Transparent transport to one official Linear MCP connection.",
    )

    async def handle_list_tools(
        request: types.ListToolsRequest,
    ) -> types.ServerResult:
        try:
            return types.ServerResult(root=await forwarder.list_tools(request))
        except BridgeError as exc:
            raise _protocol_error(exc) from None

    async def handle_call_tool(
        request: types.CallToolRequest,
    ) -> types.ServerResult:
        try:
            return types.ServerResult(root=await forwarder.call_tool(request))
        except BridgeError as exc:
            raise _protocol_error(exc) from None

    server.request_handlers[types.ListToolsRequest] = handle_list_tools
    server.request_handlers[types.CallToolRequest] = handle_call_tool
    return server


async def _serve(resolved_credential: str, timeout_seconds: float) -> None:
    headers = {"Authorization": f"Bearer {resolved_credential}"}
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=False,
        timeout=timeout_seconds,
    ) as http_client:
        async with streamable_http_client(
            LINEAR_MCP_ENDPOINT,
            http_client=http_client,
        ) as (upstream_read, upstream_write, _session_id):
            async with ClientSession(upstream_read, upstream_write) as session:
                await session.initialize()
                server = build_server(OfficialLinearForwarder(session))
                async with stdio_server() as (local_read, local_write):
                    await server.run(
                        local_read,
                        local_write,
                        server.create_initialization_options(),
                        raise_exceptions=False,
                    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--op-reference", required=True)
    serve.add_argument("--op-account", required=True)
    serve.add_argument("--op-executable", default="op")
    serve.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        provider = OnePasswordBearerProvider(
            args.op_reference,
            account=args.op_account,
            executable=args.op_executable,
            timeout_seconds=args.timeout_seconds,
        )
        resolved_credential = provider.resolve()
        asyncio.run(_serve(resolved_credential, args.timeout_seconds))
        return 0
    except BridgeError as exc:
        print(
            json.dumps({"ok": False, "error": exc.payload()}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "bridge_failed",
                        "message": "The official Linear MCP bridge failed closed.",
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
