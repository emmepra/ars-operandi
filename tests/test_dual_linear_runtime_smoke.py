from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "skills" / "dual-linear-mcp" / "scripts" / "dual_linear_mcp.py"
SMOKE = ROOT / "skills" / "dual-linear-mcp" / "scripts" / "smoke_dual_linear_mcp.py"
FIXTURE = ROOT / "skills" / "dual-linear-mcp" / "references" / "example-manifest.yaml"

spec = importlib.util.spec_from_file_location("smoke_dual_linear_mcp", SMOKE)
assert spec is not None and spec.loader is not None
smoke_dual_linear_mcp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke_dual_linear_mcp
spec.loader.exec_module(smoke_dual_linear_mcp)


FAKE_CODEX = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

if os.environ.get("CODEX_SKIP_1P_SIGNIN") != "1":
    raise SystemExit(8)
if any(key == "OP_SESSION" or key.startswith("OP_SESSION_") for key in os.environ):
    raise SystemExit(9)
if sys.argv[1:4] != ["mcp", "get", "dual-linear"]:
    raise SystemExit(3)
print(Path(os.environ["FAKE_CODEX_STATE"]).read_text(encoding="utf-8"))
"""

FAKE_OP = """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if args[0] == "signin":
    print("ephemeral-session")
    raise SystemExit(0)
if args[0] == "inject":
    template = sys.stdin.read()
    output = template.replace(
        "{{ op://Sample/credential-alpha/api-key }}", "token-alpha"
    ).replace(
        "{{ op://Sample/credential-beta/api-key }}", "token-beta"
    )
    print(output, end="")
    raise SystemExit(0)
if args[0] == "read":
    print("token-alpha" if "credential-alpha" in args[1] else "token-beta")
    raise SystemExit(0)
raise SystemExit(3)
"""


class LinearIdentityHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        token = self.headers.get("Authorization")
        suffix = "alpha" if token == "token-alpha" else "beta"
        payload = {
            "data": {
                "organization": {
                    "id": f"workspace-{suffix}",
                    "name": f"Sample {suffix.title()}",
                },
                "teams": {
                    "nodes": [
                        {
                            "id": f"team-{suffix}",
                            "key": suffix.upper(),
                            "name": f"{suffix.title()} Team",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class RuntimeSmokeTests(unittest.TestCase):
    def test_mcp_handshake_lists_tools_and_discovers_two_explicit_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_codex = root / "codex"
            fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
            fake_op = root / "op"
            fake_op.write_text(FAKE_OP, encoding="utf-8")
            fake_op.chmod(fake_op.stat().st_mode | stat.S_IXUSR)
            state = root / "codex-state.json"

            server = ThreadingHTTPServer(("127.0.0.1", 0), LinearIdentityHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/graphql"
            state.write_text(
                json.dumps(
                    {
                        "name": "dual-linear",
                        "enabled": True,
                        "transport": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": [
                                str(ADAPTER),
                                "serve",
                                "--manifest",
                                str(FIXTURE),
                                "--op-reference-template",
                                "op://Sample/{profile}/api-key",
                                "--op-executable",
                                str(fake_op),
                                "--op-auth-mode",
                                "direct",
                                "--op-account",
                                "example",
                                "--linear-endpoint",
                                endpoint,
                                "--timeout-seconds",
                                "5",
                            ],
                            "env": None,
                            "env_vars": [],
                            "cwd": None,
                        },
                    }
                ),
                encoding="utf-8",
            )
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "FAKE_CODEX_STATE": str(state),
                            "OP_SESSION": "ambient-session",
                        },
                        clear=False,
                    ),
                    mock.patch("builtins.print") as output,
                ):
                    result = smoke_dual_linear_mcp.main(
                        [
                            "--connection-alias",
                            "linear-alpha",
                            "--connection-alias",
                            "linear-beta",
                            "--codex-executable",
                            str(fake_codex),
                            "--timeout-seconds",
                            "10",
                        ]
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(result, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "runtime-verified")
        self.assertEqual(
            [item["connection_alias"] for item in payload["discoveries"]],
            ["linear-alpha", "linear-beta"],
        )
        self.assertEqual(
            [item["workspace_id"] for item in payload["discoveries"]],
            ["workspace-alpha", "workspace-beta"],
        )


if __name__ == "__main__":
    unittest.main()
