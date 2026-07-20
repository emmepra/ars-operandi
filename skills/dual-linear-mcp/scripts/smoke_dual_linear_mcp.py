#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.27,<2"]
# ///
"""Run a read-only MCP handshake and explicit Linear discovery smoke."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping


MCP_ALIAS = "dual-linear"
REQUIRED_TOOLS = frozenset(
    {"resolve_linear_route", "linear_discover", "linear_get_issue"}
)
AUTH_ENV_KEYS = frozenset(
    {
        "OP_ACCOUNT",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_SESSION",
    }
)


class SmokeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _clean_env() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in AUTH_ENV_KEYS and not key.startswith("OP_SESSION_")
    }
    environment["CODEX_SKIP_1P_SIGNIN"] = "1"
    return environment


def _load_transport(codex: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [codex, "mcp", "get", MCP_ALIAS, "--json"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env=_clean_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeError(
            "codex_config_unavailable",
            "Codex could not inspect the managed dual-linear MCP configuration.",
        ) from exc
    if completed.returncode != 0:
        raise SmokeError(
            "codex_config_unavailable",
            "Codex could not inspect the managed dual-linear MCP configuration.",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeError(
            "mcp_config_invalid",
            "Codex returned an invalid dual-linear MCP configuration.",
        ) from exc
    transport = payload.get("transport") if isinstance(payload, Mapping) else None
    if (
        not isinstance(payload, Mapping)
        or payload.get("name") != MCP_ALIAS
        or not isinstance(transport, Mapping)
        or transport.get("type") != "stdio"
        or not isinstance(transport.get("command"), str)
        or not isinstance(transport.get("args"), list)
        or not all(isinstance(item, str) for item in transport["args"])
        or transport.get("env") not in (None, {})
        or transport.get("env_vars") not in (None, [])
        or "--enable-mutations" in transport["args"]
    ):
        raise SmokeError(
            "mcp_config_invalid",
            "The dual-linear MCP configuration is not the read-only STDIO shape.",
        )
    return dict(transport)


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    candidates: list[Any] = [structured]
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                candidates.append(json.loads(text))
            except json.JSONDecodeError:
                continue
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("result"), dict):
            candidate = candidate["result"]
        if isinstance(candidate, dict) and isinstance(candidate.get("ok"), bool):
            return candidate
    raise SmokeError(
        "mcp_protocol_error",
        "The dual-linear MCP returned an invalid tool payload.",
    )


async def _run_smoke(
    transport: Mapping[str, Any],
    aliases: tuple[str, ...],
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, Any]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - inline uv metadata supplies it
        raise SmokeError(
            "dependency_missing",
            "The MCP Python SDK is required; run this script through uv.",
        ) from exc

    parameters = StdioServerParameters(
        command=transport["command"],
        args=list(transport["args"]),
        env=_clean_env(),
        cwd=transport.get("cwd"),
    )
    discoveries: list[dict[str, str]] = []
    try:
        with Path(os.devnull).open("w", encoding="utf-8") as errlog:
            async with stdio_client(parameters, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    tool_names = {tool.name for tool in tools.tools}
                    if not REQUIRED_TOOLS.issubset(tool_names):
                        raise SmokeError(
                            "mcp_tool_contract_mismatch",
                            "The dual-linear MCP tool contract is incomplete.",
                        )
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + timeout_seconds
                    for alias in aliases:
                        while True:
                            remaining = deadline - loop.time()
                            if remaining <= 0:
                                raise SmokeError(
                                    "runtime_smoke_timeout",
                                    "The read-only discovery smoke did not complete in time.",
                                )
                            result = await session.call_tool(
                                "linear_discover",
                                {"connection_alias": alias},
                                read_timeout_seconds=timedelta(
                                    seconds=min(remaining, 60.0)
                                ),
                            )
                            payload = _tool_payload(result)
                            if payload["ok"]:
                                route = payload.get("route")
                                identity = payload.get("identity")
                                if (
                                    not isinstance(route, Mapping)
                                    or route.get("connection_alias") != alias
                                    or not isinstance(identity, Mapping)
                                    or not isinstance(identity.get("workspace_id"), str)
                                    or payload.get("verification")
                                    != "workspace-identity-verified"
                                ):
                                    raise SmokeError(
                                        "runtime_identity_mismatch",
                                        "A discovery result did not match its explicit alias.",
                                    )
                                discoveries.append(
                                    {
                                        "connection_alias": alias,
                                        "workspace_id": identity["workspace_id"],
                                        "verification": payload["verification"],
                                    }
                                )
                                break
                            error = payload.get("error")
                            code = (
                                error.get("code")
                                if isinstance(error, Mapping)
                                else None
                            )
                            if code != "secret_preload_pending":
                                raise SmokeError(
                                    str(code or "runtime_discovery_failed"),
                                    "A read-only discovery failed closed; no fallback was attempted.",
                                )
                            await asyncio.sleep(min(poll_seconds, remaining))
    except SmokeError:
        raise
    except Exception as exc:
        raise SmokeError(
            "mcp_runtime_unavailable",
            "The dual-linear MCP runtime smoke failed without exposing details.",
        ) from exc
    return {
        "ok": True,
        "status": "runtime-verified",
        "alias": MCP_ALIAS,
        "tools": sorted(REQUIRED_TOOLS),
        "discoveries": discoveries,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--connection-alias",
        action="append",
        required=True,
        help="Explicit verified connection alias; repeat for every workspace.",
    )
    parser.add_argument("--codex-executable", default="codex")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    aliases = tuple(args.connection_alias)
    try:
        if len(set(aliases)) != len(aliases):
            raise SmokeError(
                "invalid_selector",
                "Runtime smoke connection aliases must not be repeated.",
            )
        if args.timeout_seconds <= 0 or args.poll_seconds <= 0:
            raise SmokeError(
                "invalid_configuration",
                "Runtime smoke timeout and poll interval must be positive.",
            )
        transport = _load_transport(args.codex_executable)
        payload = asyncio.run(
            _run_smoke(
                transport,
                aliases,
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
        )
        print(json.dumps(payload, sort_keys=True))
        return 0
    except SmokeError as exc:
        print(
            json.dumps({"ok": False, "error": exc.payload()}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
