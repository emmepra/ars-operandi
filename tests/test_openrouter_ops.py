from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "openrouter-ops" / "scripts" / "openrouter_ops.py"

spec = importlib.util.spec_from_file_location("openrouter_ops", SCRIPT)
assert spec is not None and spec.loader is not None
openrouter_ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(openrouter_ops)


class OpenRouterOpsTests(unittest.TestCase):
    def test_redact_removes_openrouter_key_values(self) -> None:
        fake_key = "sk-" + "or-v1-secret"
        payload = {
            "key": fake_key,
            "data": {"message": f"use {fake_key} here", "hash": "abc123", "creator_user_id": "user_123"},
        }
        redacted = openrouter_ops.redact(payload)
        dumped = json.dumps(redacted)
        self.assertNotIn(fake_key, dumped)
        self.assertEqual(redacted["key"], "<redacted>")
        self.assertEqual(redacted["data"]["hash"], "abc123")
        self.assertEqual(redacted["data"]["creator_user_id"], "<redacted>")

    def test_redact_text_removes_exact_runtime_key(self) -> None:
        fake_key = "sk-" + "or-v1-secret"
        redacted = openrouter_ops.redact_text(f"token={fake_key}", fake_key)
        self.assertNotIn(fake_key, redacted)
        self.assertIn("<redacted-openrouter-key>", redacted)

    def test_build_child_env_drops_management_key(self) -> None:
        child_env = openrouter_ops.build_child_env(
            {"OPENROUTER_MANAGEMENT_KEY": "management-secret", "PATH": "/bin"},
            runtime_key="sk-or-v1-runtime",
            runtime_env_name="OPENROUTER_API_KEY",
            management_env_name="OPENROUTER_MANAGEMENT_KEY",
        )
        self.assertNotIn("OPENROUTER_MANAGEMENT_KEY", child_env)
        self.assertEqual(child_env["OPENROUTER_API_KEY"], "sk-or-v1-runtime")
        self.assertEqual(child_env["PATH"], "/bin")

    def test_build_create_payload_for_workspace_uuid(self) -> None:
        args = argparse.Namespace(
            name="ariadne-smoke",
            limit=1.0,
            limit_reset="none",
            expires_at="2026-06-22T00:00:00Z",
            expires_in_days=None,
            include_byok_in_limit=False,
        )
        payload = openrouter_ops.build_create_payload(
            args,
            "550e8400-e29b-41d4-a716-446655440000",
        )
        self.assertEqual(
            payload,
            {
                "name": "ariadne-smoke",
                "include_byok_in_limit": False,
                "expires_at": "2026-06-22T00:00:00Z",
                "limit": 1.0,
                "limit_reset": None,
                "workspace_id": "550e8400-e29b-41d4-a716-446655440000",
            },
        )

    def test_create_key_dry_run_does_not_need_secrets(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "create-key",
                "--name",
                "ariadne-smoke",
                "--workspace",
                "550e8400-e29b-41d4-a716-446655440000",
                "--limit",
                "1",
                "--expires-at",
                "2026-06-22T00:00:00Z",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = json.loads(proc.stdout)
        self.assertIs(output["ok"], True)
        self.assertIs(output["live"], False)
        self.assertEqual(
            output["request"]["body"]["workspace_id"],
            "550e8400-e29b-41d4-a716-446655440000",
        )
        self.assertIn("run-ephemeral", output["next_step"])
        self.assertNotIn("sk-or-", proc.stdout)

    def test_run_ephemeral_dry_run_does_not_need_secrets(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "run-ephemeral",
                "--name",
                "ariadne-smoke",
                "--workspace",
                "550e8400-e29b-41d4-a716-446655440000",
                "--limit",
                "1",
                "--expires-at",
                "2026-06-22T00:00:00Z",
                "--",
                "echo",
                "ok",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = json.loads(proc.stdout)
        self.assertIs(output["ok"], True)
        self.assertIs(output["live"], False)
        self.assertEqual(output["operation"], "run-ephemeral")
        self.assertEqual(output["command"], ["echo", "ok"])
        self.assertEqual(output["cleanup"], {"on_exit": "delete-key"})
        self.assertNotIn("sk-or-", proc.stdout)

    def test_create_key_live_is_rejected(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "create-key",
                "--live",
                "--name",
                "ariadne-smoke",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        output = json.loads(proc.stdout)
        self.assertIs(output["ok"], False)
        self.assertIn("run-ephemeral", output["error"])


if __name__ == "__main__":
    unittest.main()
