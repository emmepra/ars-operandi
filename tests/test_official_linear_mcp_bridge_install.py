from __future__ import annotations

import contextlib
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "official-linear-mcp-bridge"
    / "scripts"
    / "install_official_linear_mcp_bridge.py"
)

spec = importlib.util.spec_from_file_location(
    "install_official_linear_mcp_bridge", SCRIPT
)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = installer
spec.loader.exec_module(installer)


INITIAL_CONFIG = b"""# unknown bytes stay byte-identical
model = "configured-model"

[features]
unknown_future_key = true

[mcp_servers.existing]
command = "existing-command"
args = ["unchanged"]
"""
CONNECTION_ARGS = [
    "--connection",
    "linear-example-a=op://Example/linear-a/credential",
    "--connection",
    "linear-example-b=op://Example/linear-b/credential",
    "--op-account",
    "example.1password.com",
]
EXPECTED_ALIASES = ["linear-example-a", "linear-example-b"]


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skills_root = self.root / ".agents" / "skills"
        self.config = self.root / ".codex" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(INITIAL_CONFIG)
        self.config.chmod(0o640)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(
        self,
        command: str,
        *,
        apply: bool = False,
        resume_paused: bool = False,
    ) -> list[str]:
        values = [
            command,
            "--skills-root",
            str(self.skills_root),
            "--config",
            str(self.config),
        ]
        if command == "install":
            values.extend(CONNECTION_ARGS)
            if resume_paused:
                values.append("--resume-paused")
        if apply:
            values.append("--apply")
        return values

    def pause_managed_connections(self) -> bytes:
        destination = self.skills_root / installer.SKILL_NAME
        marker = json.loads(
            (destination / installer.MARKER_NAME).read_text(encoding="utf-8")
        )
        active_segment = marker["config_segment"].encode("utf-8")
        paused_segment = active_segment.replace(b"enabled = true", b"enabled = false")
        self.assertEqual(active_segment.count(b"enabled = true"), len(EXPECTED_ALIASES))
        current = self.config.read_bytes()
        self.assertEqual(current.count(active_segment), 1)
        paused = current.replace(active_segment, paused_segment)
        self.config.write_bytes(paused)
        return paused

    def invoke(
        self, argv: list[str], *, mock_runtime: bool = True
    ) -> tuple[int, dict[str, object]]:
        runtime_context = (
            mock.patch.object(installer, "_prepare_runtime")
            if mock_runtime
            else contextlib.nullcontext()
        )
        with mock.patch("builtins.print") as output, runtime_context as runtime:
            result = installer.main(argv)
        self.runtime_mock = runtime
        payload = json.loads(output.call_args.args[0])
        return result, payload

    def test_dry_run_plans_two_canaries_and_makes_no_writes(self) -> None:
        before = self.config.read_bytes()

        result, payload = self.invoke(self.args("install"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["aliases"], EXPECTED_ALIASES)
        self.assertIn("prepare-credential-free-runtime", payload["actions"])
        self.assertIn("verify-configured-offline-runtime", payload["actions"])
        self.assertNotIn("op://", json.dumps(payload))
        self.runtime_mock.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.skills_root.exists())

    def test_apply_is_idempotent_preserves_unknown_bytes_mode_and_exact_refs(
        self,
    ) -> None:
        first_result, first = self.invoke(self.args("install", apply=True))
        applied = self.config.read_bytes()
        second_result, second = self.invoke(self.args("install", apply=True))

        self.assertEqual(first_result, 0)
        self.assertEqual(first["status"], "installed")
        self.assertEqual(second_result, 0)
        self.assertEqual(second["status"], "verified")
        self.assertEqual(self.config.read_bytes(), applied)
        self.assertTrue(applied.startswith(INITIAL_CONFIG))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        destination = self.skills_root / installer.SKILL_NAME
        self.assertTrue((destination / "SKILL.md").is_file())
        marker_path = destination / installer.MARKER_NAME
        self.assertTrue(marker_path.is_file())
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)

        parsed = tomllib.loads(applied.decode("utf-8"))
        servers = parsed["mcp_servers"]
        expected = {
            "linear-example-a": "op://Example/linear-a/credential",
            "linear-example-b": "op://Example/linear-b/credential",
        }
        for alias, reference in expected.items():
            config = servers[alias]
            self.assertEqual(config["command"], "uv")
            self.assertEqual(config["args"][:3], ["run", "--offline", "--script"])
            self.assertIn("--op-reference", config["args"])
            self.assertIn(reference, config["args"])
            self.assertIn("--op-account", config["args"])
            self.assertIn("example.1password.com", config["args"])
            self.assertEqual(config["default_tools_approval_mode"], "writes")
            self.assertEqual(config["startup_timeout_sec"], 60.0)
            self.assertNotIn("env", config)

    def test_apply_hardens_a_legacy_marker_without_changing_its_content(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        marker_path = self.skills_root / installer.SKILL_NAME / installer.MARKER_NAME
        original = marker_path.read_bytes()
        marker_path.chmod(0o644)

        result, payload = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "updated")
        self.assertEqual(marker_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)

    def test_failed_update_restores_the_prior_marker_with_private_mode(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        destination = self.skills_root / installer.SKILL_NAME
        marker_path = destination / installer.MARKER_NAME
        original = marker_path.read_bytes()
        prior_config = self.config.read_bytes()
        marker_path.chmod(0o644)
        changed_args = self.args("install", apply=True)
        changed_args[changed_args.index("example.1password.com")] = (
            "other.1password.com"
        )

        with mock.patch.object(
            installer, "_atomic_write", side_effect=OSError("simulated config failure")
        ):
            result, payload = self.invoke(changed_args)

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertEqual(marker_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), 0o600)
        self.assertEqual(self.config.read_bytes(), prior_config)

    def test_rollback_is_dry_run_first_and_removes_exact_managed_targets(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        destination = self.skills_root / installer.SKILL_NAME

        dry_result, dry = self.invoke(self.args("rollback"))
        self.assertEqual(dry_result, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertTrue(destination.exists())
        self.assertNotEqual(self.config.read_bytes(), INITIAL_CONFIG)

        apply_result, applied = self.invoke(self.args("rollback", apply=True))
        self.assertEqual(apply_result, 0)
        self.assertEqual(applied["status"], "removed")
        self.assertEqual(self.config.read_bytes(), INITIAL_CONFIG)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertFalse(destination.exists())

    def test_preexisting_alias_conflict_fails_closed_without_writes(self) -> None:
        conflict = (
            INITIAL_CONFIG + b'\n[mcp_servers.linear-example-a]\ncommand = "other"\n'
        )
        self.config.write_bytes(conflict)

        result, payload = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "connection_alias_conflict")
        self.assertEqual(self.config.read_bytes(), conflict)
        self.assertFalse(self.skills_root.exists())

    def test_managed_config_or_skill_drift_refuses_apply_and_rollback(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        installed = self.config.read_text(encoding="utf-8")
        self.config.write_text(
            installed.replace('command = "uv"', 'command = "drifted"', 1),
            encoding="utf-8",
        )
        drifted_config = self.config.read_bytes()

        result, payload = self.invoke(self.args("rollback", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertEqual(self.config.read_bytes(), drifted_config)

        destination = self.skills_root / installer.SKILL_NAME
        (destination / "SKILL.md").write_text("local drift", encoding="utf-8")
        result, payload = self.invoke(self.args("install", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_skill_drift")
        self.assertEqual((destination / "SKILL.md").read_text(), "local drift")

    def test_unrelated_changes_after_apply_survive_exact_target_rollback(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        with self.config.open("ab") as handle:
            handle.write(b'\n[future_section]\nfuture_key = "preserved"\n')

        result, payload = self.invoke(self.args("rollback", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "removed")
        self.assertEqual(
            self.config.read_bytes(),
            INITIAL_CONFIG + b'\n[future_section]\nfuture_key = "preserved"\n',
        )

    def test_config_replacement_failure_restores_the_prior_skill_state(self) -> None:
        original_replace = installer.os.replace

        def fail_config(source: object, destination: object) -> None:
            if Path(destination) == self.config.resolve():
                raise OSError("simulated config failure")
            original_replace(source, destination)

        with mock.patch.object(installer.os, "replace", side_effect=fail_config):
            result, payload = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertEqual(self.config.read_bytes(), INITIAL_CONFIG)
        self.assertFalse((self.skills_root / installer.SKILL_NAME).exists())

    def test_install_and_rollback_restore_config_without_final_newline(self) -> None:
        original = INITIAL_CONFIG.rstrip(b"\n")
        self.config.write_bytes(original)

        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        marker = json.loads(
            (self.skills_root / installer.SKILL_NAME / installer.MARKER_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(marker["config_segment"].startswith("\n"))
        self.assertEqual(self.invoke(self.args("rollback", apply=True))[0], 0)

        self.assertEqual(self.config.read_bytes(), original)

    def test_connection_inputs_are_strict_and_references_are_not_printed(self) -> None:
        cases = (
            ["--connection", "Bad_Alias=op://Example/item/credential"],
            ["--connection", "linear-example=https://example.invalid/credential"],
            [
                "--connection",
                "linear-example=op://Example/item/credential",
                "--connection",
                "linear-example=op://Example/other/credential",
            ],
            [
                "--connection",
                "linear-example-a=op://Example/item/credential",
                "--connection",
                "linear-example-b=op://Example/item/credential",
            ],
        )
        for connection_args in cases:
            with self.subTest(connection_args=connection_args):
                argv = [
                    "install",
                    "--skills-root",
                    str(self.skills_root),
                    "--config",
                    str(self.config),
                    *connection_args,
                    "--op-account",
                    "example.1password.com",
                    "--apply",
                ]
                result, payload = self.invoke(argv)
                self.assertEqual(result, 2)
                self.assertEqual(payload["error"]["code"], "invalid_connection")
                self.assertNotIn("op://", json.dumps(payload))

        invalid_account = self.args("install", apply=True)
        invalid_account[invalid_account.index("example.1password.com")] = "invalid"
        result, payload = self.invoke(invalid_account)
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "invalid_account")

    def test_runtime_prepare_then_offline_verify_precede_config_apply(self) -> None:
        events: list[str] = []
        original_atomic_write = installer._atomic_write

        def run_runtime(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            events.append("offline" if "--offline" in command else "prepare")
            self.assertEqual(command[-1], "--help")
            self.assertFalse(any(value.startswith("op://") for value in command))
            self.assertNotIn("example.1password.com", command)
            environment = kwargs["env"]
            self.assertFalse(any(key.startswith("OP_SESSION") for key in environment))
            return subprocess.CompletedProcess(command, 0, "ignored", "ignored")

        def write_config(path: Path, data: bytes, mode: int) -> None:
            events.append("config")
            original_atomic_write(path, data, mode)

        with (
            mock.patch.object(installer.subprocess, "run", side_effect=run_runtime),
            mock.patch.object(installer, "_atomic_write", side_effect=write_config),
        ):
            result, payload = self.invoke(
                self.args("install", apply=True), mock_runtime=False
            )

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "installed")
        self.assertEqual(events, ["prepare", "offline", "config"])

    def test_runtime_failures_leave_config_and_skill_unchanged(self) -> None:
        cases = (
            (
                [subprocess.CompletedProcess([], 1, "private", "private")],
                "runtime_prepare_failed",
                1,
            ),
            (
                [
                    subprocess.CompletedProcess([], 0, "ignored", "ignored"),
                    subprocess.CompletedProcess([], 1, "private", "private"),
                ],
                "runtime_offline_failed",
                2,
            ),
        )
        for results, code, calls in cases:
            with self.subTest(code=code):
                with mock.patch.object(
                    installer.subprocess, "run", side_effect=results
                ) as run:
                    result, payload = self.invoke(
                        self.args("install", apply=True), mock_runtime=False
                    )
                self.assertEqual(result, 2)
                self.assertEqual(payload["error"]["code"], code)
                self.assertNotIn("private", json.dumps(payload))
                self.assertEqual(run.call_count, calls)
                self.assertEqual(self.config.read_bytes(), INITIAL_CONFIG)
                self.assertFalse((self.skills_root / installer.SKILL_NAME).exists())

    def test_noop_apply_still_prepares_and_verifies_offline(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        completed = subprocess.CompletedProcess([], 0, "ignored", "ignored")
        with (
            mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.object(installer, "_atomic_write") as write_config,
        ):
            result, payload = self.invoke(
                self.args("install", apply=True), mock_runtime=False
            )

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("--offline", run.call_args_list[0].args[0])
        self.assertIn("--offline", run.call_args_list[1].args[0])
        write_config.assert_not_called()

    def test_paused_managed_block_is_refused_without_explicit_resume(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        paused = self.pause_managed_connections()

        result, payload = self.invoke(self.args("install"))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertEqual(self.config.read_bytes(), paused)

    def test_resume_paused_dry_run_is_sanitized_and_makes_no_writes(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        paused = self.pause_managed_connections()
        destination = self.skills_root / installer.SKILL_NAME
        skill_before = {
            path.relative_to(destination): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }

        result, payload = self.invoke(self.args("install", resume_paused=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["aliases"], EXPECTED_ALIASES)
        self.assertEqual(
            payload["actions"],
            [
                "install-or-update-skill",
                "prepare-credential-free-runtime",
                "verify-configured-offline-runtime",
                "resume-exact-paused-connection-block",
            ],
        )
        self.assertNotIn("op://", json.dumps(payload))
        self.runtime_mock.assert_not_called()
        self.assertEqual(self.config.read_bytes(), paused)
        self.assertEqual(
            {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            },
            skill_before,
        )

    def test_resume_apply_preserves_unrelated_bytes_mode_and_enables_aliases(
        self,
    ) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        self.pause_managed_connections()
        unrelated = b'\n[future_section]\nfuture_key = "preserved"\n'
        with self.config.open("ab") as handle:
            handle.write(unrelated)
        paused = self.config.read_bytes()
        prefix = paused[: paused.index(installer.BLOCK_BEGIN.encode("utf-8"))]

        result, payload = self.invoke(
            self.args("install", apply=True, resume_paused=True)
        )

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "resumed")
        resumed = self.config.read_bytes()
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertTrue(resumed.startswith(prefix))
        self.assertTrue(resumed.endswith(unrelated))
        self.assertEqual(resumed.count(b"enabled = true"), len(EXPECTED_ALIASES))
        self.assertNotIn(b"enabled = false", resumed)
        parsed = tomllib.loads(resumed.decode("utf-8"))
        for alias in EXPECTED_ALIASES:
            self.assertIs(parsed["mcp_servers"][alias]["enabled"], True)

    def test_resume_failures_restore_old_skill_and_leave_config_paused(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        paused = self.pause_managed_connections()
        destination = self.skills_root / installer.SKILL_NAME
        old_skill = {
            path.relative_to(destination): path.read_bytes()
            for path in destination.rglob("*")
            if path.is_file()
        }

        with mock.patch.object(
            installer,
            "_prepare_runtime",
            side_effect=installer.InstallError("runtime_prepare_failed", "sanitized"),
        ):
            result, payload = self.invoke(
                self.args("install", apply=True, resume_paused=True),
                mock_runtime=False,
            )
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "runtime_prepare_failed")
        self.assertEqual(self.config.read_bytes(), paused)
        self.assertEqual(
            {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            },
            old_skill,
        )

        original_atomic_write = installer._atomic_write
        config_write_calls = 0

        def write_then_fail_once(path: Path, data: bytes, mode: int) -> None:
            nonlocal config_write_calls
            config_write_calls += 1
            original_atomic_write(path, data, mode)
            if config_write_calls == 1:
                raise OSError("config failed after replacement")

        with mock.patch.object(
            installer, "_atomic_write", side_effect=write_then_fail_once
        ):
            result, payload = self.invoke(
                self.args("install", apply=True, resume_paused=True)
            )
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("config failed", json.dumps(payload))
        self.assertEqual(config_write_calls, 2)
        self.assertEqual(self.config.read_bytes(), paused)
        self.assertEqual(
            {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            },
            old_skill,
        )

    def test_resume_refuses_identity_mismatch_and_any_other_managed_drift(
        self,
    ) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        paused = self.pause_managed_connections()

        mismatch = self.args("install", resume_paused=True)
        mismatch[mismatch.index("example.1password.com")] = "other.1password.com"
        result, payload = self.invoke(mismatch)
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "resume_identity_mismatch")
        self.assertEqual(self.config.read_bytes(), paused)

        drifted = paused.replace(
            b"startup_timeout_sec = 60.0", b"startup_timeout_sec = 61.0", 1
        )
        self.config.write_bytes(drifted)
        result, payload = self.invoke(self.args("install", resume_paused=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertEqual(self.config.read_bytes(), drifted)


if __name__ == "__main__":
    unittest.main()
