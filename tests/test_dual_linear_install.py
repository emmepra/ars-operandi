from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "dual-linear-mcp" / "scripts" / "install_dual_linear.py"

spec = importlib.util.spec_from_file_location("install_dual_linear", SCRIPT)
assert spec is not None and spec.loader is not None
install_dual_linear = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = install_dual_linear
spec.loader.exec_module(install_dual_linear)


FAKE_CODEX = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_CODEX_STATE"])
args = sys.argv[1:]
if os.environ.get("CODEX_SKIP_1P_SIGNIN") != "1":
    raise SystemExit(8)
if any(
    key == "OP_SESSION" or key.startswith("OP_SESSION_")
    for key in os.environ
):
    raise SystemExit(9)
if args[:2] == ["mcp", "get"]:
    if not state_path.exists():
        print("Error: No MCP server named 'dual-linear' found.", file=sys.stderr)
        raise SystemExit(1)
    print(state_path.read_text(encoding="utf-8"))
    raise SystemExit(0)
if args[:2] == ["mcp", "add"]:
    alias = args[2]
    separator = args.index("--")
    command = args[separator + 1]
    command_args = args[separator + 2:]
    payload = {
        "name": alias,
        "enabled": True,
        "disabled_reason": None,
        "transport": {
            "type": "stdio",
            "command": command,
            "args": command_args,
            "env": None,
            "env_vars": [],
            "cwd": None,
        },
        "enabled_tools": None,
        "disabled_tools": None,
        "startup_timeout_sec": None,
        "tool_timeout_sec": None,
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    raise SystemExit(0)
if args[:3] == ["mcp", "remove", "dual-linear"]:
    state_path.unlink(missing_ok=True)
    raise SystemExit(0)
raise SystemExit(3)
"""


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / ".agents" / "skills"
        self.state_path = self.root / "codex-state.json"
        self.fake_codex = self.root / "codex"
        self.fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.fake_codex.chmod(
            self.fake_codex.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )
        (self.root / "projects.yaml").write_text("projects: []\n", encoding="utf-8")
        self.environment = {"FAKE_CODEX_STATE": str(self.state_path)}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install_args(
        self, *, apply: bool = False, account: str = "example"
    ) -> list[str]:
        args = [
            "install",
            "--skills-root",
            str(self.skills_root),
            "--codex-executable",
            str(self.fake_codex),
            "--manifest",
            str(self.root / "projects.yaml"),
            "--op-reference-template",
            "op://Example/{profile}/credential",
            "--op-account",
            account,
        ]
        if apply:
            args.append("--apply")
        return args

    def invoke(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            mock.patch.object(install_dual_linear, "_prewarm_adapter"),
            mock.patch("builtins.print") as output,
        ):
            result = install_dual_linear.main(argv)
        return result, json.loads(output.call_args.args[0])

    def test_install_dry_run_makes_no_writes(self) -> None:
        result, payload = self.invoke(self.install_args())

        self.assertEqual(result, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertFalse(self.skills_root.exists())
        self.assertFalse(self.state_path.exists())

    def test_apply_is_idempotent_and_uses_exact_public_alias_and_config(self) -> None:
        first_result, first_payload = self.invoke(self.install_args(apply=True))
        second_result, second_payload = self.invoke(self.install_args(apply=True))

        destination = (self.skills_root / "dual-linear-mcp").resolve()
        config = json.loads(self.state_path.read_text(encoding="utf-8"))
        args = config["transport"]["args"]
        self.assertEqual(first_result, 0)
        self.assertEqual(first_payload["status"], "installed")
        self.assertEqual(second_result, 0)
        self.assertEqual(second_payload["status"], "no-op")
        self.assertTrue((destination / "SKILL.md").is_file())
        self.assertTrue((destination / install_dual_linear.MARKER_NAME).is_file())
        self.assertEqual(config["name"], "dual-linear")
        self.assertEqual(config["transport"]["type"], "stdio")
        self.assertEqual(config["transport"]["command"], "uv")
        self.assertIsNone(config["transport"]["env"])
        self.assertEqual(config["transport"]["env_vars"], [])
        self.assertIn(str(destination / "scripts" / "dual_linear_mcp.py"), args)
        self.assertIn("--op-auth-mode", args)
        self.assertIn("ephemeral", args)
        self.assertEqual(args[:3], ["run", "--offline", "--script"])
        self.assertNotIn("--enable-mutations", args)
        self.assertFalse(any("token" in value.lower() for value in args))

        smoke_result, smoke_payload = self.invoke(
            [
                "smoke",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.fake_codex),
            ]
        )
        self.assertEqual(smoke_result, 0)
        self.assertEqual(smoke_payload["status"], "config-verified")

    def test_codex_subprocess_environment_bypasses_wrapper_signin(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "OP_SESSION": "ambient-session",
                "OP_SESSION_example": "ambient-session-two",
                "OP_SERVICE_ACCOUNT_TOKEN": "ambient-service-token",
            },
            clear=True,
        ):
            environment = install_dual_linear._subprocess_env()

        self.assertEqual(environment["CODEX_SKIP_1P_SIGNIN"], "1")
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("OP_SESSION", environment)
        self.assertNotIn("OP_SESSION_example", environment)
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", environment)

    def test_unmanaged_destination_conflict_fails_closed(self) -> None:
        destination = self.skills_root / "dual-linear-mcp"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("unmanaged", encoding="utf-8")

        result, payload = self.invoke(self.install_args(apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "unmanaged_skill_conflict")
        self.assertEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"), "unmanaged"
        )
        self.assertFalse(self.state_path.exists())

    def test_managed_mcp_config_drift_fails_closed(self) -> None:
        self.assertEqual(self.invoke(self.install_args(apply=True))[0], 0)
        drifted = json.loads(self.state_path.read_text(encoding="utf-8"))
        drifted["transport"]["command"] = "other-command"
        self.state_path.write_text(json.dumps(drifted), encoding="utf-8")

        result, payload = self.invoke(self.install_args(apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "mcp_config_conflict")
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["transport"]["command"], "other-command")

    def test_managed_desired_state_update_is_safe(self) -> None:
        self.assertEqual(self.invoke(self.install_args(apply=True))[0], 0)

        result, payload = self.invoke(
            self.install_args(apply=True, account="example-next")
        )

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "updated")
        config = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("example-next", config["transport"]["args"])
        marker = json.loads(
            (
                self.skills_root / "dual-linear-mcp" / install_dual_linear.MARKER_NAME
            ).read_text(encoding="utf-8")
        )
        self.assertIn("example-next", marker["mcp_config"]["transport"]["args"])

    def test_managed_skill_content_drift_fails_closed(self) -> None:
        self.assertEqual(self.invoke(self.install_args(apply=True))[0], 0)
        skill_file = self.skills_root / "dual-linear-mcp" / "SKILL.md"
        skill_file.write_text("local drift", encoding="utf-8")

        result, payload = self.invoke(self.install_args(apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_skill_drift")
        self.assertEqual(skill_file.read_text(encoding="utf-8"), "local drift")

    def test_rollback_is_dry_run_first_and_removes_only_managed_state(self) -> None:
        self.assertEqual(self.invoke(self.install_args(apply=True))[0], 0)
        destination = self.skills_root / "dual-linear-mcp"

        dry_result, dry_payload = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.fake_codex),
            ]
        )
        self.assertEqual(dry_result, 0)
        self.assertEqual(dry_payload["mode"], "dry-run")
        self.assertTrue(destination.exists())
        self.assertTrue(self.state_path.exists())

        apply_result, apply_payload = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.fake_codex),
                "--apply",
            ]
        )
        self.assertEqual(apply_result, 0)
        self.assertEqual(apply_payload["status"], "removed")
        self.assertFalse(destination.exists())
        self.assertFalse(self.state_path.exists())

    def test_rollback_rejects_unmanaged_skill(self) -> None:
        destination = self.skills_root / "dual-linear-mcp"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("unmanaged", encoding="utf-8")

        result, payload = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.fake_codex),
                "--apply",
            ]
        )

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "unmanaged_skill_conflict")
        self.assertTrue(destination.exists())


if __name__ == "__main__":
    unittest.main()
