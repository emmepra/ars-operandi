from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import stat
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


INITIAL_CONFIG = b"""# unrelated bytes stay byte-identical
model = "configured-model"

[features]
unknown_future_key = true

[mcp_servers.existing]
command = "existing-command"
args = ["unchanged"]
"""
ALIASES = ("linear-example-a", "linear-example-b")
ALIAS_ARGS = ["--alias", ALIASES[0], "--alias", ALIASES[1]]


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

    @property
    def destination(self) -> Path:
        return self.skills_root / installer.SKILL_NAME

    @property
    def state_path(self) -> Path:
        return self.skills_root / installer.STAGED_STATE_NAME

    def args(
        self,
        command: str,
        *,
        apply: bool = False,
        aliases: tuple[str, ...] = ALIASES,
        expected_config_sha256: str | None = None,
    ) -> list[str]:
        values = [
            command,
            "--skills-root",
            str(self.skills_root),
            "--config",
            str(self.config),
        ]
        if command in {"install", "migrate", "recover-migrate"}:
            for alias in aliases:
                values.extend(["--alias", alias])
        if expected_config_sha256 is not None:
            values.extend(["--expected-config-sha256", expected_config_sha256])
        if apply:
            values.append("--apply")
        return values

    def invoke(self, argv: list[str]) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = installer.main(argv)
        rendered = stdout.getvalue() + stderr.getvalue()
        payload = json.loads(stdout.getvalue() or stderr.getvalue())
        return result, payload, rendered

    def snapshot(self, root: Path) -> dict[str, tuple[bytes, int]]:
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in root.rglob("*")
            if path.is_file()
        }

    def make_legacy_install(self, *, preexisting_keyring: bool = False) -> bytes:
        self.destination.mkdir(parents=True)
        (self.destination / "scripts").mkdir()
        (self.destination / "SKILL.md").write_text(
            "legacy bridge skill\n", encoding="utf-8"
        )
        (self.destination / "scripts" / "official_linear_mcp_bridge.py").write_text(
            "# legacy credential transport runtime\n", encoding="utf-8"
        )
        legacy_block = (
            f"{installer.LEGACY_BLOCK_BEGIN}\n"
            "[mcp_servers.linear-old-a]\n"
            'command = "uv"\n'
            'args = ["run", "--script", "legacy", '
            '"op://Example/linear-a/credential"]\n'
            "enabled = true\n\n"
            "[mcp_servers.linear-old-b]\n"
            'command = "uv"\n'
            'args = ["run", "--script", "legacy", '
            '"op://Example/linear-b/credential"]\n'
            "enabled = true\n\n"
            f"{installer.LEGACY_BLOCK_END}\n"
        ).encode("utf-8")
        base = INITIAL_CONFIG
        if preexisting_keyring:
            base = b'mcp_oauth_credentials_store = "keyring"\n' + base
        segment = installer._managed_segment(base, legacy_block)
        original_config = base + segment
        self.config.write_bytes(original_config)
        self.config.chmod(0o640)
        source_hash = installer._tree_hash(self.destination)
        marker = {
            "installer": installer.MARKER_ID,
            "version": installer.LEGACY_MARKER_VERSION,
            "source_hash": source_hash,
            "connections": [
                {
                    "alias": "linear-old-a",
                    "reference": "op://Example/linear-a/credential",
                },
                {
                    "alias": "linear-old-b",
                    "reference": "op://Example/linear-b/credential",
                },
            ],
            "op_account": "example.1password.com",
            "config_segment": segment.decode("utf-8"),
            "config_segment_sha256": hashlib.sha256(segment).hexdigest(),
        }
        marker_path = self.destination / installer.MARKER_NAME
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker_path.chmod(0o600)
        return original_config

    def make_recovery_preimage(self) -> bytes:
        exact_v2 = self.make_legacy_install()
        marker = json.loads(
            (self.destination / installer.MARKER_NAME).read_text(encoding="utf-8")
        )
        legacy_segment = marker["config_segment"].encode("utf-8")
        base = exact_v2[: -len(legacy_segment)]
        command = (
            "/example/runtimes/mcp-remote/ars-operandi-1.0.0/"
            "node_modules/.bin/mcp-remote"
        )
        runtime_pair = b"".join(
            (
                f"[mcp_servers.{alias}]\n"
                f'command = "{command}"\n'
                f'args = ["{installer.LINEAR_MCP_ENDPOINT}", "{39117 + index}", "--transport", "http-only", "--host", "127.0.0.1"]\n'
                'default_tools_approval_mode = "writes"\n'
                "startup_timeout_sec = 60.0\n\n"
                f"[mcp_servers.{alias}.env]\n"
                'ARS_MCP_AUTH_MODE = "noninteractive"\n'
                f'MCP_REMOTE_CONFIG_DIR = "/example/profile-{index}"\n\n'
            ).encode()
            for index, alias in enumerate(ALIASES)
        )
        paused = installer._paused_legacy_body(marker)
        interleaved = (
            b"[mcp_servers.unrelated-between]\n"
            b'command = "preserved"\n\n'
            b"[future_after_canaries]\n"
            b'value = "byte-exact"\n\n'
        )
        end_line = (installer.LEGACY_BLOCK_END + "\n").encode()
        composite = base + runtime_pair + paused + interleaved + end_line
        self.config.write_bytes(composite)
        self.config.chmod(0o640)
        return composite

    def assert_native_config(self, data: bytes) -> None:
        parsed = tomllib.loads(data.decode("utf-8"))
        self.assertEqual(parsed["mcp_oauth_credentials_store"], "keyring")
        expected = {
            "url": installer.LINEAR_MCP_ENDPOINT,
            "auth": "oauth",
            "enabled": True,
            "default_tools_approval_mode": "writes",
            "startup_timeout_sec": 60.0,
        }
        for alias in ALIASES:
            self.assertEqual(parsed["mcp_servers"][alias], expected)
            self.assertNotIn("required", parsed["mcp_servers"][alias])
            self.assertNotIn("command", parsed["mcp_servers"][alias])
            self.assertNotIn("args", parsed["mcp_servers"][alias])
            self.assertNotIn("env", parsed["mcp_servers"][alias])
            self.assertNotIn("http_headers", parsed["mcp_servers"][alias])

    def assert_redacted(self, rendered: str) -> None:
        for value in (
            *ALIASES,
            "op://Example/linear-a/credential",
            "op://Example/linear-b/credential",
            "example.1password.com",
        ):
            self.assertNotIn(value, rendered)

    def test_clean_install_is_dry_run_first_redacted_and_write_free(self) -> None:
        before = self.config.read_bytes()

        result, payload, rendered = self.invoke(self.args("install"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["alias_count"], 2)
        self.assertEqual(len(payload["alias_fingerprints"]), 2)
        self.assertIn("add-keyring-invariant", payload["actions"])
        self.assertIn("add-exact-native-connection-block", payload["actions"])
        self.assert_redacted(rendered)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.skills_root.exists())

    def test_clean_apply_installs_only_native_config_management_state(self) -> None:
        result, payload, rendered = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "installed")
        applied = self.config.read_bytes()
        self.assert_native_config(applied)
        self.assertIn(INITIAL_CONFIG, applied)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertTrue((self.destination / "SKILL.md").is_file())
        marker = self.destination / installer.MARKER_NAME
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertTrue(
            json.loads(marker.read_text(encoding="utf-8"))["config_preexisted"]
        )
        self.assertFalse(
            (self.destination / "scripts" / "official_linear_mcp_bridge.py").exists()
        )
        installed_source = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.destination.rglob("*.py")
        ).lower()
        self.assertNotIn("op inject", installed_source)
        self.assertNotIn("subprocess", installed_source)
        self.assert_redacted(rendered)

    def test_clean_restore_recreates_original_config_absence(self) -> None:
        self.config.unlink()

        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        marker = json.loads(
            (self.destination / installer.MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertFalse(marker["config_preexisted"])
        self.assertTrue(self.config.is_file())

        result, payload, rendered = self.invoke(self.args("restore", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "removed")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.destination.exists())
        self.assert_redacted(rendered)

    def test_clean_restore_keeps_later_unrelated_config_when_original_was_absent(
        self,
    ) -> None:
        self.config.unlink()
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        unrelated = b'\n[future_section]\nfuture_key = "preserved"\n'
        with self.config.open("ab") as handle:
            handle.write(unrelated)

        self.assertEqual(self.invoke(self.args("restore", apply=True))[0], 0)

        self.assertTrue(self.config.is_file())
        self.assertEqual(self.config.read_bytes(), unrelated)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_clean_post_replace_failure_restores_original_config_absence(self) -> None:
        self.config.unlink()
        real_atomic_write = installer._atomic_write

        def write_then_fail(path: Path, data: bytes, mode: int) -> None:
            real_atomic_write(path, data, mode)
            raise OSError("private post-replace detail")

        with mock.patch.object(installer, "_atomic_write", side_effect=write_then_fail):
            result, payload, rendered = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("private post-replace detail", rendered)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.destination.exists())

    def test_clean_apply_is_idempotent_and_refuses_alias_change(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        installed = self.config.read_bytes()

        result, payload, _rendered = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "verified")
        self.assertEqual(self.config.read_bytes(), installed)

        result, payload, rendered = self.invoke(
            self.args(
                "install",
                apply=True,
                aliases=("linear-other-a", "linear-other-b"),
            )
        )
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "alias_set_mismatch")
        self.assertNotIn("linear-other", rendered)
        self.assertEqual(self.config.read_bytes(), installed)

    def test_preexisting_keyring_is_accepted_and_never_removed(self) -> None:
        original = b'mcp_oauth_credentials_store = "keyring"\n' + INITIAL_CONFIG
        self.config.write_bytes(original)

        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        applied = self.config.read_bytes()
        self.assertEqual(applied.count(b"mcp_oauth_credentials_store"), 1)
        self.assertEqual(self.invoke(self.args("restore", apply=True))[0], 0)

        self.assertEqual(self.config.read_bytes(), original)

    def test_auto_file_and_other_oauth_stores_are_refused_without_writes(self) -> None:
        for value in ("auto", "file", "unexpected"):
            with self.subTest(value=value):
                conflict = (
                    f'mcp_oauth_credentials_store = "{value}"\n'.encode()
                    + INITIAL_CONFIG
                )
                self.config.write_bytes(conflict)

                result, payload, rendered = self.invoke(
                    self.args("install", apply=True)
                )

                self.assertEqual(result, 2)
                self.assertEqual(payload["error"]["code"], "oauth_store_conflict")
                self.assertNotIn(value, rendered)
                self.assertEqual(self.config.read_bytes(), conflict)
                self.assertFalse(self.skills_root.exists())

    def test_alias_validation_and_unmanaged_conflicts_fail_closed(self) -> None:
        for aliases in (
            ("secret-one",),
            ("secret-one", "secret-two", "secret-three"),
            ("duplicate-sensitive", "duplicate-sensitive"),
            ("Sensitive_Alias", "valid-sensitive"),
        ):
            with self.subTest(aliases=aliases):
                result, payload, rendered = self.invoke(
                    self.args("install", apply=True, aliases=aliases)
                )
                self.assertEqual(result, 2)
                self.assertEqual(payload["error"]["code"], "invalid_aliases")
                for alias in aliases:
                    self.assertNotIn(alias, rendered)
                self.assertEqual(self.config.read_bytes(), INITIAL_CONFIG)

        conflict = (
            INITIAL_CONFIG
            + b'\n[mcp_servers.linear-example-a]\nurl = "https://other.invalid"\n'
        )
        self.config.write_bytes(conflict)
        result, payload, _rendered = self.invoke(self.args("install", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "connection_alias_conflict")
        self.assertEqual(self.config.read_bytes(), conflict)

    def test_restore_preserves_unrelated_bytes_mode_and_missing_final_newline(
        self,
    ) -> None:
        original = INITIAL_CONFIG.rstrip(b"\n")
        self.config.write_bytes(original)
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        unrelated = b'\n[future_section]\nfuture_key = "preserved"\n'
        with self.config.open("ab") as handle:
            handle.write(unrelated)

        dry_result, dry, _rendered = self.invoke(self.args("restore"))
        self.assertEqual(dry_result, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertTrue(self.destination.exists())

        result, payload, _rendered = self.invoke(self.args("restore", apply=True))
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "removed")
        self.assertEqual(self.config.read_bytes(), original + unrelated)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertFalse(self.destination.exists())

    def test_managed_config_and_skill_drift_are_refused(self) -> None:
        self.assertEqual(self.invoke(self.args("install", apply=True))[0], 0)
        installed = self.config.read_bytes()
        drifted = installed.replace(b'auth = "oauth"', b'auth = "other"', 1)
        self.config.write_bytes(drifted)

        result, payload, _rendered = self.invoke(self.args("restore", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertEqual(self.config.read_bytes(), drifted)

        self.config.write_bytes(installed)
        (self.destination / "SKILL.md").write_text("local drift", encoding="utf-8")
        result, payload, _rendered = self.invoke(self.args("install", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_skill_drift")

    def test_clean_config_failure_restores_config_and_skill_atomically(self) -> None:
        before = self.config.read_bytes()
        with mock.patch.object(
            installer, "_atomic_write", side_effect=OSError("private failure")
        ):
            result, payload, rendered = self.invoke(self.args("install", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("private failure", rendered)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.destination.exists())

    def test_v2_migration_is_dry_run_first_and_leaves_legacy_exact(self) -> None:
        original = self.make_legacy_install()
        legacy_before = self.snapshot(self.destination)

        result, payload, rendered = self.invoke(self.args("migrate"))

        self.assertEqual(result, 0)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertIn("stage-exact-v2-rollback-authority", payload["actions"])
        self.assertIn("replace-v2-block-with-native-oauth-block", payload["actions"])
        self.assert_redacted(rendered)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertFalse(self.state_path.exists())

    def test_v2_migration_rejects_marker_account_reference_and_identity_drift(
        self,
    ) -> None:
        original_config = self.make_legacy_install()
        marker_path = self.destination / installer.MARKER_NAME
        original_marker = json.loads(marker_path.read_text(encoding="utf-8"))

        def missing_account(payload: dict[str, object]) -> None:
            payload.pop("op_account")

        def invalid_account(payload: dict[str, object]) -> None:
            payload["op_account"] = "private invalid account"

        def invalid_reference(payload: dict[str, object]) -> None:
            payload["connections"][0]["reference"] = "private-invalid-reference"

        def duplicate_alias(payload: dict[str, object]) -> None:
            payload["connections"][1]["alias"] = payload["connections"][0]["alias"]

        def duplicate_reference(payload: dict[str, object]) -> None:
            payload["connections"][1]["reference"] = payload["connections"][0][
                "reference"
            ]

        cases = (
            ("missing-account", missing_account),
            ("invalid-account", invalid_account),
            ("invalid-reference", invalid_reference),
            ("duplicate-alias", duplicate_alias),
            ("duplicate-reference", duplicate_reference),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                candidate = json.loads(json.dumps(original_marker))
                mutate(candidate)
                marker_path.write_text(
                    json.dumps(candidate, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                marker_path.chmod(0o600)

                result, payload, rendered = self.invoke(self.args("migrate"))

                self.assertEqual(result, 2)
                self.assertEqual(payload["error"]["code"], "legacy_state_invalid")
                self.assertNotIn("private", rendered)
                self.assertNotIn("op://", rendered)
                self.assertEqual(self.config.read_bytes(), original_config)
                self.assertFalse(self.state_path.exists())

        marker_path.write_text(
            json.dumps(original_marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_path.chmod(0o600)

    def test_v2_migrate_apply_activates_native_and_keeps_rollback_runtime(self) -> None:
        self.make_legacy_install()
        legacy_before = self.snapshot(self.destination)

        result, payload, rendered = self.invoke(self.args("migrate", apply=True))

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "staged")
        self.assert_native_config(self.config.read_bytes())
        self.assertNotIn(b"op://", self.config.read_bytes())
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertTrue(
            (self.destination / "scripts" / "official_linear_mcp_bridge.py").exists()
        )
        self.assertTrue(self.state_path.is_dir())
        state_text = (self.state_path / installer.MARKER_NAME).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("op://", state_text)
        self.assertNotIn("example.1password.com", state_text)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assert_redacted(rendered)

    def test_staged_restore_returns_exact_v2_config_and_preserves_new_bytes(
        self,
    ) -> None:
        original = self.make_legacy_install()
        legacy_before = self.snapshot(self.destination)
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        unrelated = b'\n[future_section]\nfuture_key = "preserved"\n'
        with self.config.open("ab") as handle:
            handle.write(unrelated)

        dry_result, dry, _rendered = self.invoke(self.args("restore"))
        self.assertEqual(dry_result, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertTrue(self.state_path.exists())

        result, payload, rendered = self.invoke(self.args("restore", apply=True))
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "legacy-restored")
        self.assertEqual(self.config.read_bytes(), original + unrelated)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assert_redacted(rendered)

    def test_staged_restore_post_replace_failure_restores_native_state(self) -> None:
        self.make_legacy_install()
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        native_config = self.config.read_bytes()
        config_mode = stat.S_IMODE(self.config.stat().st_mode)
        legacy_before = self.snapshot(self.destination)
        legacy_mode = stat.S_IMODE(self.destination.stat().st_mode)
        state_before = self.snapshot(self.state_path)
        state_mode = stat.S_IMODE(self.state_path.stat().st_mode)
        real_atomic_write = installer._atomic_write
        calls = 0

        def write_then_fail_once(path: Path, data: bytes, mode: int) -> None:
            nonlocal calls
            calls += 1
            real_atomic_write(path, data, mode)
            if calls == 1:
                raise OSError("private staged restore detail")

        with mock.patch.object(
            installer, "_atomic_write", side_effect=write_then_fail_once
        ):
            result, payload, rendered = self.invoke(self.args("restore", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "restore_failed")
        self.assertNotIn("private staged restore detail", rendered)
        self.assertEqual(calls, 2)
        self.assertEqual(self.config.read_bytes(), native_config)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), config_mode)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), legacy_mode)
        self.assertEqual(self.snapshot(self.state_path), state_before)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), state_mode)

    def test_migration_with_preexisting_keyring_restores_without_removing_it(
        self,
    ) -> None:
        original = self.make_legacy_install(preexisting_keyring=True)
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        self.assertEqual(
            self.config.read_bytes().count(b"mcp_oauth_credentials_store"), 1
        )

        self.assertEqual(self.invoke(self.args("restore", apply=True))[0], 0)

        self.assertEqual(self.config.read_bytes(), original)

    def test_migration_refuses_v2_config_skill_and_alias_drift(self) -> None:
        original = self.make_legacy_install()
        self.config.write_bytes(
            original.replace(b"startup_timeout_sec", b"startup_timeout_secs")
            if b"startup_timeout_sec" in original
            else original.replace(b"enabled = true", b"enabled = false", 1)
        )
        drifted = self.config.read_bytes()

        result, payload, _rendered = self.invoke(self.args("migrate", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertEqual(self.config.read_bytes(), drifted)

        self.config.write_bytes(original)
        (self.destination / "SKILL.md").write_text("drift", encoding="utf-8")
        result, payload, _rendered = self.invoke(self.args("migrate", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_skill_drift")

    def test_migration_config_failure_removes_state_and_restores_legacy(self) -> None:
        original = self.make_legacy_install()
        legacy_before = self.snapshot(self.destination)
        with mock.patch.object(
            installer, "_atomic_write", side_effect=OSError("sensitive detail")
        ):
            result, payload, rendered = self.invoke(self.args("migrate", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("sensitive detail", rendered)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertFalse(self.state_path.exists())

    def test_migration_failure_after_replacement_restores_exact_prior_bytes(
        self,
    ) -> None:
        original = self.make_legacy_install()
        legacy_before = self.snapshot(self.destination)
        real_atomic_write = installer._atomic_write
        calls = 0

        def write_then_fail_once(path: Path, data: bytes, mode: int) -> None:
            nonlocal calls
            calls += 1
            real_atomic_write(path, data, mode)
            if calls == 1:
                raise OSError("sensitive post-replace failure")

        with mock.patch.object(
            installer, "_atomic_write", side_effect=write_then_fail_once
        ):
            result, payload, rendered = self.invoke(self.args("migrate", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("sensitive post-replace failure", rendered)
        self.assertEqual(calls, 2)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertFalse(self.state_path.exists())

    def test_recovery_migration_is_digest_bound_and_restores_exact_preimage(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        legacy_before = self.snapshot(self.destination)

        dry_result, dry, rendered = self.invoke(self.args("recover-migrate"))

        self.assertEqual(dry_result, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertEqual(dry["preimage_sha256"], hashlib.sha256(original).hexdigest())
        self.assertIn("stage-owner-only-byte-exact-recovery-copy", dry["actions"])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.state_path.exists())
        self.assert_redacted(rendered)

        mismatch_result, mismatch, _rendered = self.invoke(
            self.args(
                "recover-migrate",
                apply=True,
                expected_config_sha256="0" * 64,
            )
        )
        self.assertEqual(mismatch_result, 2)
        self.assertEqual(mismatch["error"]["code"], "recovery_preimage_changed")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.state_path.exists())

        result, payload, rendered = self.invoke(
            self.args(
                "recover-migrate",
                apply=True,
                expected_config_sha256=dry["preimage_sha256"],
            )
        )
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "staged")
        self.assert_native_config(self.config.read_bytes())
        self.assertNotIn(b"mcp-remote", self.config.read_bytes())
        self.assertNotIn(installer.LEGACY_BLOCK_END.encode(), self.config.read_bytes())
        self.assertIn(
            b"[mcp_servers.unrelated-between]\n"
            b'command = "preserved"\n\n'
            b"[future_after_canaries]\n"
            b'value = "byte-exact"\n\n',
            self.config.read_bytes(),
        )
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        rollback = self.state_path / installer.RECOVERY_PREIMAGE_NAME
        self.assertEqual(rollback.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(rollback.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o700)
        self.assert_redacted(rendered)

        restore_result, restore, rendered = self.invoke(
            self.args("restore", apply=True)
        )
        self.assertEqual(restore_result, 0)
        self.assertEqual(restore["status"], "legacy-restored")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertFalse(self.state_path.exists())
        self.assert_redacted(rendered)

    def test_recovery_migration_rejects_near_miss_preimages_without_writes(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        variants = {
            "begin-restored": original.replace(
                b"# END ars-operandi official-linear-mcp-bridge connections v2",
                b"# BEGIN ars-operandi official-linear-mcp-bridge connections v2\n"
                b"# END ars-operandi official-linear-mcp-bridge connections v2",
            ),
            "active-canary": original.replace(b"enabled = false", b"enabled = true", 1),
            "floating-runtime": original.replace(
                b"ars-operandi-1.0.0", b"ars-operandi-1.0.1", 1
            ),
            "duplicate-profile": original.replace(
                b"/example/profile-1", b"/example/profile-0", 1
            ),
            "custom-header": original.replace(
                b"startup_timeout_sec = 60.0\n",
                b'startup_timeout_sec = 60.0\nhttp_headers = { Authorization = "private" }\n',
                1,
            ),
            "unexpected-enabled": original.replace(
                b"startup_timeout_sec = 60.0\n",
                b"startup_timeout_sec = 60.0\nenabled = true\n",
                1,
            ),
            "inline-env": original.replace(
                b"[mcp_servers.linear-example-a.env]\n"
                b'ARS_MCP_AUTH_MODE = "noninteractive"\n'
                b'MCP_REMOTE_CONFIG_DIR = "/example/profile-0"\n',
                b'env = { ARS_MCP_AUTH_MODE = "noninteractive", '
                b'MCP_REMOTE_CONFIG_DIR = "/example/profile-0" }\n',
                1,
            ),
            "wrong-transport": original.replace(b"http-only", b"sse-only", 1),
            "wrong-endpoint": original.replace(
                b"https://mcp.linear.app/mcp", b"https://example.invalid/mcp", 1
            ),
        }
        for name, candidate in variants.items():
            with self.subTest(name=name):
                self.config.write_bytes(candidate)
                result, payload, rendered = self.invoke(
                    self.args(
                        "recover-migrate",
                        apply=True,
                        expected_config_sha256=hashlib.sha256(candidate).hexdigest(),
                    )
                )
                self.assertEqual(result, 2)
                self.assertIn(
                    payload["error"]["code"],
                    {"recovery_preimage_invalid", "recovery_preimage_unsafe"},
                )
                self.assertNotIn("private", rendered)
                self.assertEqual(self.config.read_bytes(), candidate)
                self.assertFalse(self.state_path.exists())

    def test_recovery_apply_failure_removes_private_copy_and_restores_preimage(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        digest = hashlib.sha256(original).hexdigest()
        with mock.patch.object(
            installer,
            "_atomic_compare_and_write",
            side_effect=OSError("private recovery failure"),
        ):
            result, payload, rendered = self.invoke(
                self.args(
                    "recover-migrate",
                    apply=True,
                    expected_config_sha256=digest,
                )
            )

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "config_apply_failed")
        self.assertNotIn("private recovery failure", rendered)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.state_path.exists())

    def test_recovery_apply_rejects_drift_observable_at_final_comparison(self) -> None:
        original = self.make_recovery_preimage()
        concurrent = original + b"\n# concurrent unrelated change\n"
        real_replace = installer._replace_locked_config

        def change_then_replace(
            temporary: Path,
            path: Path,
            descriptor: int,
            expected: bytes,
            expected_mode: int,
        ) -> None:
            self.config.write_bytes(concurrent)
            real_replace(temporary, path, descriptor, expected, expected_mode)

        with mock.patch.object(
            installer, "_replace_locked_config", side_effect=change_then_replace
        ):
            result, payload, _rendered = self.invoke(
                self.args(
                    "recover-migrate",
                    apply=True,
                    expected_config_sha256=hashlib.sha256(original).hexdigest(),
                )
            )

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "recovery_preimage_changed")
        self.assertEqual(self.config.read_bytes(), concurrent)
        self.assertFalse(self.state_path.exists())

    def test_recovery_restore_rejects_drift_observable_at_final_comparison(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        self.assertEqual(
            self.invoke(
                self.args(
                    "recover-migrate",
                    apply=True,
                    expected_config_sha256=hashlib.sha256(original).hexdigest(),
                )
            )[0],
            0,
        )
        native = self.config.read_bytes()
        concurrent = native + b"\n# concurrent restore change\n"
        state_before = self.snapshot(self.state_path)
        real_replace = installer._replace_locked_config

        def change_then_replace(
            temporary: Path,
            path: Path,
            descriptor: int,
            expected: bytes,
            expected_mode: int,
        ) -> None:
            self.config.write_bytes(concurrent)
            real_replace(temporary, path, descriptor, expected, expected_mode)

        with mock.patch.object(
            installer, "_replace_locked_config", side_effect=change_then_replace
        ):
            result, payload, _rendered = self.invoke(self.args("restore", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "recovery_preimage_changed")
        self.assertEqual(self.config.read_bytes(), concurrent)
        self.assertEqual(self.snapshot(self.state_path), state_before)

        self.config.write_bytes(native)
        rollback = self.state_path / installer.RECOVERY_PREIMAGE_NAME
        rollback.chmod(0o640)
        result, payload, _rendered = self.invoke(self.args("restore"))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_state_drift")
        self.assertEqual(self.config.read_bytes(), native)

    def test_recovery_finalize_removes_rollback_copy_and_rejects_native_drift(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        digest = hashlib.sha256(original).hexdigest()
        self.assertEqual(
            self.invoke(
                self.args(
                    "recover-migrate",
                    apply=True,
                    expected_config_sha256=digest,
                )
            )[0],
            0,
        )
        native = self.config.read_bytes()
        self.config.write_bytes(native + b"\n# unrelated post-stage drift\n")
        result, payload, _rendered = self.invoke(self.args("finalize", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertTrue((self.state_path / installer.RECOVERY_PREIMAGE_NAME).exists())

        self.config.write_bytes(native)
        result, payload, rendered = self.invoke(self.args("finalize", apply=True))
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "finalized")
        self.assertFalse(self.state_path.exists())
        self.assertFalse(
            (self.destination / "scripts" / "official_linear_mcp_bridge.py").exists()
        )
        self.assert_redacted(rendered)

    def test_recovery_finalize_rejects_observable_drift_and_restores_authority(
        self,
    ) -> None:
        original = self.make_recovery_preimage()
        self.assertEqual(
            self.invoke(
                self.args(
                    "recover-migrate",
                    apply=True,
                    expected_config_sha256=hashlib.sha256(original).hexdigest(),
                )
            )[0],
            0,
        )
        native = self.config.read_bytes()
        concurrent = native + b"\n# concurrent finalize change\n"
        legacy_before = self.snapshot(self.destination)
        state_before = self.snapshot(self.state_path)
        real_install_staged = installer._install_staged

        def install_then_change(staged: Path, destination: Path) -> Path | None:
            backup = real_install_staged(staged, destination)
            self.config.write_bytes(concurrent)
            return backup

        with mock.patch.object(
            installer, "_install_staged", side_effect=install_then_change
        ):
            result, payload, _rendered = self.invoke(self.args("finalize", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "recovery_preimage_changed")
        self.assertEqual(self.config.read_bytes(), concurrent)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertEqual(self.snapshot(self.state_path), state_before)

    def test_recovery_docs_require_quiescence_readback_and_disclose_residual_race(
        self,
    ) -> None:
        skill = (ROOT / "skills" / installer.SKILL_NAME / "SKILL.md").read_text(
            encoding="utf-8"
        )
        runbook = (
            ROOT
            / "skills"
            / installer.SKILL_NAME
            / "references"
            / "installation-and-operations.md"
        ).read_text(encoding="utf-8")
        combined = skill + "\n" + runbook
        for required in (
            "close Codex Settings",
            "avoid manual config writes",
            "Immediately afterward, read back",
            "`flock`",
            "non-cooperating writer",
            "residual comparison-to-replace race remains",
            "drift detected within the documented",
            "failed-operation mismatch restores staged",
            "must not assume that\nrollback authority still exists",
            "post-finalization\nrecovery boundary",
        ):
            self.assertIn(required, combined)

    def test_finalize_is_dry_run_first_then_replaces_legacy_runtime(self) -> None:
        self.make_legacy_install()
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        native_config = self.config.read_bytes()
        legacy_before = self.snapshot(self.destination)

        dry_result, dry, rendered = self.invoke(self.args("finalize"))
        self.assertEqual(dry_result, 0)
        self.assertEqual(dry["mode"], "dry-run")
        self.assertIn(
            "replace-legacy-runtime-with-credential-free-config-skill",
            dry["actions"],
        )
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertTrue(self.state_path.exists())
        self.assert_redacted(rendered)

        result, payload, rendered = self.invoke(self.args("finalize", apply=True))
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "finalized")
        self.assertEqual(self.config.read_bytes(), native_config)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(
            (self.destination / "scripts" / "official_linear_mcp_bridge.py").exists()
        )
        marker = json.loads(
            (self.destination / installer.MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["version"], 3)
        self.assertEqual(marker["lifecycle"], "finalized-v2")
        installed_source = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.destination.rglob("*.py")
        ).lower()
        self.assertNotIn("op inject", installed_source)
        self.assertNotIn("subprocess", installed_source)
        self.assert_redacted(rendered)

    def test_finalize_refuses_native_or_legacy_drift(self) -> None:
        self.make_legacy_install()
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        native = self.config.read_bytes()
        self.config.write_bytes(
            native.replace(
                b"startup_timeout_sec = 60.0", b"startup_timeout_sec = 61.0", 1
            )
        )

        result, payload, _rendered = self.invoke(self.args("finalize", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_config_drift")
        self.assertTrue(self.state_path.exists())

        self.config.write_bytes(native)
        (self.destination / "SKILL.md").write_text("drift", encoding="utf-8")
        result, payload, _rendered = self.invoke(self.args("finalize", apply=True))
        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "managed_skill_drift")
        self.assertTrue(self.state_path.exists())

    def test_finalize_filesystem_failure_restores_staged_authority(self) -> None:
        self.make_legacy_install()
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        native = self.config.read_bytes()
        legacy_before = self.snapshot(self.destination)
        original_replace = installer.os.replace

        def fail_state_move(source: object, destination: object) -> None:
            if Path(source).name == installer.STAGED_STATE_NAME:
                raise OSError("private state failure")
            original_replace(source, destination)

        with mock.patch.object(installer.os, "replace", side_effect=fail_state_move):
            result, payload, rendered = self.invoke(self.args("finalize", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "finalize_failed")
        self.assertNotIn("private state failure", rendered)
        self.assertEqual(self.config.read_bytes(), native)
        self.assertEqual(self.snapshot(self.destination), legacy_before)
        self.assertTrue(self.state_path.exists())

    def test_finalized_restore_post_replace_failure_restores_native_state(
        self,
    ) -> None:
        self.make_legacy_install()
        self.assertEqual(self.invoke(self.args("migrate", apply=True))[0], 0)
        self.assertEqual(self.invoke(self.args("finalize", apply=True))[0], 0)
        native_config = self.config.read_bytes()
        config_mode = stat.S_IMODE(self.config.stat().st_mode)
        skill_before = self.snapshot(self.destination)
        skill_mode = stat.S_IMODE(self.destination.stat().st_mode)
        real_atomic_write = installer._atomic_write
        calls = 0

        def write_then_fail_once(path: Path, data: bytes, mode: int) -> None:
            nonlocal calls
            calls += 1
            real_atomic_write(path, data, mode)
            if calls == 1:
                raise OSError("private finalized restore detail")

        with mock.patch.object(
            installer, "_atomic_write", side_effect=write_then_fail_once
        ):
            result, payload, rendered = self.invoke(self.args("restore", apply=True))

        self.assertEqual(result, 2)
        self.assertEqual(payload["error"]["code"], "restore_failed")
        self.assertNotIn("private finalized restore detail", rendered)
        self.assertEqual(calls, 2)
        self.assertEqual(self.config.read_bytes(), native_config)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), config_mode)
        self.assertEqual(self.snapshot(self.destination), skill_before)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), skill_mode)

    def test_installer_has_no_runtime_network_process_or_login_behavior(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8").lower()
        for forbidden in (
            "subprocess",
            "httpx",
            "requests",
            "urllib",
            "socket",
            "webbrowser",
            "codex mcp login",
            "op inject",
            "restart codex",
        ):
            self.assertNotIn(forbidden, source)
        self.assertFalse((SCRIPT.parent / "official_linear_mcp_bridge.py").exists())


if __name__ == "__main__":
    unittest.main()
