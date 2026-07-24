from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_mail_runtime import write_index


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_mail_runtime.py"
SPEC = importlib.util.spec_from_file_location("install_mail_runtime", SCRIPT)
assert SPEC and SPEC.loader
install_mail_runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_mail_runtime)


class MailRuntimeInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / "skills"
        self.project_index = write_index(self.root)
        self.config_root = self.root / "provider-state"
        self.state = self.root / "mcp.json"
        self.previous_uv_cache = os.environ.get("UV_CACHE_DIR")
        os.environ["UV_CACHE_DIR"] = str(self.root / "uv-cache")
        self.codex = self.root / "codex"
        self.codex.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys

state = pathlib.Path(%r)
args = sys.argv[1:]
if args[:3] == ['mcp', 'get', 'ars-mail']:
    if not state.exists():
        print('No MCP server named ars-mail', file=sys.stderr)
        raise SystemExit(1)
    print(state.read_text())
    raise SystemExit(0)
if args[:3] == ['mcp', 'remove', 'ars-mail']:
    state.unlink(missing_ok=True)
    raise SystemExit(0)
if args[:3] == ['mcp', 'add', 'ars-mail']:
    marker = args.index('--')
    command = args[marker + 1]
    transport_args = args[marker + 2:]
    payload = {
        'name': 'ars-mail',
        'enabled': True,
        'transport': {
            'type': 'stdio',
            'command': command,
            'args': transport_args,
            'env': None,
            'env_vars': [],
            'cwd': None,
        },
    }
    state.write_text(json.dumps(payload))
    raise SystemExit(0)
raise SystemExit(2)
"""
            % str(self.state),
            encoding="utf-8",
        )
        self.codex.chmod(0o755)

    def tearDown(self) -> None:
        if self.previous_uv_cache is None:
            os.environ.pop("UV_CACHE_DIR", None)
        else:
            os.environ["UV_CACHE_DIR"] = self.previous_uv_cache
        self.temporary.cleanup()

    def install_args(self, *, apply: bool = False, replace: bool = False) -> list[str]:
        args = [
            "install",
            "--runtime-repo",
            str(REPO_ROOT),
            "--project-index",
            str(self.project_index),
            "--config-root",
            str(self.config_root),
            "--skills-root",
            str(self.skills_root),
            "--codex-executable",
            str(self.codex),
        ]
        if replace:
            args.append("--replace-existing-skill")
        if apply:
            args.append("--apply")
        return args

    def invoke(self, args: list[str]) -> tuple[int, dict]:
        # main prints JSON; capture it without depending on a test CLI framework.
        import contextlib
        import io

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = install_mail_runtime.main(args)
        raw = stdout.getvalue() if code == 0 else stderr.getvalue()
        return code, json.loads(raw)

    def test_dry_run_apply_discovery_and_rollback_are_idempotent(self) -> None:
        dry_code, dry = self.invoke(self.install_args())
        self.assertEqual(dry_code, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertFalse(self.state.exists())
        self.assertFalse((self.skills_root / "mailctl-email-access").exists())

        apply_code, applied = self.invoke(self.install_args(apply=True))
        self.assertEqual(apply_code, 0, applied)
        self.assertEqual(applied["status"], "installed")
        self.assertTrue(self.state.is_file())
        self.assertTrue(
            (
                self.skills_root
                / "mailctl-email-access"
                / install_mail_runtime.MARKER_NAME
            ).is_file()
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["name"], "ars-mail")
        self.assertIn(str(REPO_ROOT), state["transport"]["args"])
        self.assertNotIn("workflow-agent", " ".join(state["transport"]["args"]))

        second_code, second = self.invoke(self.install_args(apply=True))
        self.assertEqual(second_code, 0, second)
        self.assertEqual(second["status"], "no-op")

        smoke_code, smoke = self.invoke(
            [
                "smoke",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.codex),
            ]
        )
        self.assertEqual(smoke_code, 0, smoke)
        self.assertEqual(
            smoke["checks"], ["managed-skill", "single-mcp-alias", "offline-runtime"]
        )

        rollback_dry_code, rollback_dry = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.codex),
            ]
        )
        self.assertEqual(rollback_dry_code, 0)
        self.assertEqual(rollback_dry["mode"], "dry-run")
        self.assertTrue(self.state.exists())

        rollback_code, rollback = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.codex),
                "--apply",
            ]
        )
        self.assertEqual(rollback_code, 0, rollback)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.skills_root / "mailctl-email-access").exists())

    def test_unmanaged_skill_and_mcp_fail_closed(self) -> None:
        destination = self.skills_root / "mailctl-email-access"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("unmanaged", encoding="utf-8")
        code, payload = self.invoke(self.install_args())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "unmanaged_skill_conflict")

        destination.rename(self.root / "aside")
        self.state.write_text(
            json.dumps(
                {
                    "name": "ars-mail",
                    "enabled": True,
                    "transport": {
                        "type": "stdio",
                        "command": "other",
                        "args": [],
                        "env": None,
                        "env_vars": [],
                        "cwd": None,
                    },
                }
            ),
            encoding="utf-8",
        )
        code, payload = self.invoke(self.install_args())
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "unmanaged_mcp_conflict")

    def test_explicit_replacement_is_backed_up_and_rollback_restores_it(self) -> None:
        destination = self.skills_root / "mailctl-email-access"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("preexisting", encoding="utf-8")

        code, payload = self.invoke(self.install_args(apply=True, replace=True))
        self.assertEqual(code, 0, payload)
        self.assertNotEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"), "preexisting"
        )

        code, payload = self.invoke(
            [
                "rollback",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.codex),
                "--apply",
            ]
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"), "preexisting"
        )

    def test_changed_consumer_paths_update_mcp_marker_smoke_and_idempotence(
        self,
    ) -> None:
        code, payload = self.invoke(self.install_args(apply=True))
        self.assertEqual(code, 0, payload)
        destination = self.skills_root / "mailctl-email-access"
        first_marker = json.loads(
            (destination / install_mail_runtime.MARKER_NAME).read_text(encoding="utf-8")
        )

        next_root = self.root / "next-consumer"
        next_root.mkdir()
        self.project_index = write_index(next_root)
        self.config_root = next_root / "provider-state"

        dry_code, dry = self.invoke(self.install_args())
        self.assertEqual(dry_code, 0, dry)
        self.assertEqual(
            dry["actions"],
            ["install-or-update-skill", "register-or-update-ars-mail-mcp"],
        )
        apply_code, applied = self.invoke(self.install_args(apply=True))
        self.assertEqual(apply_code, 0, applied)

        state = json.loads(self.state.read_text(encoding="utf-8"))
        marker = json.loads(
            (destination / install_mail_runtime.MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["source_hash"], first_marker["source_hash"])
        self.assertEqual(marker["mcp_config"], state)
        self.assertIn(str(self.project_index.resolve()), state["transport"]["args"])
        self.assertIn(str(self.config_root.resolve()), state["transport"]["args"])

        smoke_code, smoke = self.invoke(
            [
                "smoke",
                "--skills-root",
                str(self.skills_root),
                "--codex-executable",
                str(self.codex),
            ]
        )
        self.assertEqual(smoke_code, 0, smoke)
        repeat_code, repeat = self.invoke(self.install_args(apply=True))
        self.assertEqual(repeat_code, 0, repeat)
        self.assertEqual(repeat["status"], "no-op")

    def test_rollback_rename_failure_restores_mcp_and_preserves_skill(self) -> None:
        code, payload = self.invoke(self.install_args(apply=True))
        self.assertEqual(code, 0, payload)
        destination = self.skills_root / "mailctl-email-access"
        state_before = self.state.read_text(encoding="utf-8")
        hash_before = install_mail_runtime._tree_hash(destination)
        real_replace = install_mail_runtime.os.replace

        def fail_remove(source, target):
            if Path(source).resolve() == destination.resolve() and Path(
                target
            ).name.startswith(".ars-mail-remove-"):
                raise OSError("injected rename failure")
            return real_replace(source, target)

        with patch.object(install_mail_runtime.os, "replace", side_effect=fail_remove):
            rollback_code, rollback = self.invoke(
                [
                    "rollback",
                    "--skills-root",
                    str(self.skills_root),
                    "--codex-executable",
                    str(self.codex),
                    "--apply",
                ]
            )

        self.assertEqual(rollback_code, 2)
        self.assertEqual(rollback["error"]["code"], "skill_remove_failed")
        self.assertEqual(self.state.read_text(encoding="utf-8"), state_before)
        self.assertTrue(destination.is_dir())
        self.assertEqual(install_mail_runtime._tree_hash(destination), hash_before)


if __name__ == "__main__":
    unittest.main()
