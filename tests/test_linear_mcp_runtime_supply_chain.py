from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "skills" / "linear-mcp-runtime" / "runtime"
SCRIPT = ROOT / "scripts" / "verify_linear_runtime_supply_chain.py"

spec = importlib.util.spec_from_file_location(
    "verify_linear_runtime_supply_chain", SCRIPT
)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)


def _audit_report(*, high: int = 0, critical: int = 0) -> dict[str, object]:
    total = high + critical
    vulnerabilities: dict[str, object] = {}
    if high:
        vulnerabilities["example-high"] = {"severity": "high"}
    if critical:
        vulnerabilities["example-critical"] = {"severity": "critical"}
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": high,
                "critical": critical,
                "total": total,
            }
        },
    }


class SupplyChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        generated = subprocess.run(
            [
                "npm",
                "sbom",
                "--package-lock-only",
                "--omit=dev",
                "--sbom-format=cyclonedx",
            ],
            cwd=RUNTIME_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        cls.sbom = json.loads(generated.stdout)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        shutil.copy2(RUNTIME_ROOT / "package.json", self.runtime / "package.json")
        shutil.copy2(
            RUNTIME_ROOT / "package-lock.json", self.runtime / "package-lock.json"
        )
        self.sbom_path = self.write("sbom.json", copy.deepcopy(self.sbom))
        self.audit_path = self.write("audit.json", _audit_report())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def read(self, filename: str) -> dict[str, object]:
        return json.loads((self.runtime / filename).read_text(encoding="utf-8"))

    def write(self, filename: str, payload: dict[str, object]) -> Path:
        path = self.runtime / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def assert_code(self, expected: str, callback: object) -> None:
        with self.assertRaises(verifier.VerificationError) as raised:
            callback()  # type: ignore[operator]
        self.assertEqual(raised.exception.code, expected)

    def verify(self, **kwargs: object) -> dict[str, object]:
        kwargs.setdefault("sbom_path", self.sbom_path)
        kwargs.setdefault("audit_path", self.audit_path)
        return verifier.verify(self.runtime, **kwargs)

    def test_exact_manifest_lock_sbom_and_clean_audit_pass(self) -> None:
        result = self.verify()

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mcp_remote"], "0.1.38")
        self.assertEqual(result["undici"], "7.29.0")
        self.assertEqual(result["package_count"], 81)
        self.assertEqual(result["sbom_components"], 81)
        self.assertEqual(result["audit"]["high"], 0)  # type: ignore[index]
        self.assertEqual(result["audit"]["critical"], 0)  # type: ignore[index]

    def test_verify_requires_sbom_and_audit_evidence(self) -> None:
        self.assert_code(
            "sbom_evidence_required",
            lambda: self.verify(sbom_path=None),
        )
        self.assert_code(
            "audit_evidence_required",
            lambda: self.verify(audit_path=None),
        )

    def test_cli_requires_sbom_and_audit_evidence(self) -> None:
        cases = (
            ["--audit-report", str(self.audit_path)],
            ["--sbom", str(self.sbom_path)],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    verifier.main(["--runtime-root", str(self.runtime), *arguments])
                self.assertEqual(raised.exception.code, 2)

    def test_changed_mcp_remote_manifest_version_fails(self) -> None:
        package = self.read("package.json")
        package["dependencies"]["mcp-remote"] = "0.1.39"  # type: ignore[index]
        self.write("package.json", package)

        self.assert_code("mcp_remote_manifest_mismatch", self.verify)

    def test_changed_undici_override_fails(self) -> None:
        package = self.read("package.json")
        package["overrides"]["undici"] = "7.29.1"  # type: ignore[index]
        self.write("package.json", package)

        self.assert_code("undici_override_mismatch", self.verify)

    def test_unpinned_production_dependency_fails(self) -> None:
        package = self.read("package.json")
        package["dependencies"]["example"] = "^1.0.0"  # type: ignore[index]
        self.write("package.json", package)

        self.assert_code("runtime_dependency_unpinned", self.verify)

    def test_changed_mcp_remote_lock_version_fails(self) -> None:
        lock = self.read("package-lock.json")
        lock["packages"]["node_modules/mcp-remote"]["version"] = "0.1.39"  # type: ignore[index]
        self.write("package-lock.json", lock)

        self.assert_code("mcp_remote_lock_mismatch", self.verify)

    def test_changed_mcp_remote_integrity_fails(self) -> None:
        lock = self.read("package-lock.json")
        lock["packages"]["node_modules/mcp-remote"]["integrity"] = (  # type: ignore[index]
            "sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="
        )
        self.write("package-lock.json", lock)

        self.assert_code("mcp_remote_lock_mismatch", self.verify)

    def test_missing_transitive_integrity_fails(self) -> None:
        lock = self.read("package-lock.json")
        del lock["packages"]["node_modules/accepts"]["integrity"]  # type: ignore[index]
        self.write("package-lock.json", lock)

        self.assert_code("runtime_integrity_invalid", self.verify)

    def test_other_lock_drift_fails_exact_digest(self) -> None:
        lock = self.read("package-lock.json")
        lock["packages"]["node_modules/accepts"]["funding"] = {  # type: ignore[index]
            "url": "https://example.invalid"
        }
        self.write("package-lock.json", lock)

        self.assert_code("runtime_lock_digest_mismatch", self.verify)

    def test_missing_sbom_component_fails(self) -> None:
        sbom = copy.deepcopy(self.sbom)
        sbom["components"] = [  # type: ignore[index]
            component
            for component in sbom["components"]  # type: ignore[index]
            if component["name"] != "undici"
        ]
        sbom_path = self.write("sbom.json", sbom)

        self.assert_code(
            "sbom_components_mismatch",
            lambda: self.verify(sbom_path=sbom_path),
        )

    def test_changed_sbom_integrity_fails(self) -> None:
        sbom = copy.deepcopy(self.sbom)
        component = next(  # type: ignore[call-overload]
            item
            for item in sbom["components"]
            if item["name"] == "mcp-remote"  # type: ignore[index]
        )
        component["hashes"][0]["content"] = "0" * 128
        sbom_path = self.write("sbom.json", sbom)

        self.assert_code(
            "sbom_component_mismatch",
            lambda: self.verify(sbom_path=sbom_path),
        )

    def test_high_or_critical_audit_fails(self) -> None:
        for level in ("high", "critical"):
            with self.subTest(level=level):
                audit_path = self.write("audit.json", _audit_report(**{level: 1}))
                self.assert_code(
                    "audit_high_or_critical",
                    lambda: self.verify(audit_path=audit_path),
                )


if __name__ == "__main__":
    unittest.main()
