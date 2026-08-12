#!/usr/bin/env python3
"""Fail-closed verification for the pinned Linear MCP npm runtime."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = ROOT / "skills" / "linear-mcp-runtime" / "runtime"

MCP_REMOTE_VERSION = "0.1.38"
MCP_REMOTE_RESOLVED = "https://registry.npmjs.org/mcp-remote/-/mcp-remote-0.1.38.tgz"
MCP_REMOTE_INTEGRITY = (
    "sha512-w+JU4U3CfG29TawXR4JLNQ9d1Un5nT8AGI65f/juCaqUdF/"
    "V6fS7wE4o7xNPbB8X58o46hRXEJgYglQMAKQs4w=="
)
UNDICI_VERSION = "7.29.0"
UNDICI_RESOLVED = "https://registry.npmjs.org/undici/-/undici-7.29.0.tgz"
UNDICI_INTEGRITY = (
    "sha512-IDxfleLmmbSskfWSUATiN1nfn2rDuvnMOqb5CWR92iIfojA0Ud+"
    "ulOAAEQ57LPr9rWmsreUyf5lwyao+7GNNVw=="
)
EXPECTED_PACKAGE_SHA256 = (
    "a873c785dd3b308203ac50183246879a57e8f2d9d7da01736920d5705ef749c1"
)
EXPECTED_LOCK_SHA256 = (
    "859999971ac11fd3035d6a7dc2df9a7334fa7b6adc065c9a6ada468e7a28c5f9"
)

_EXACT_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
_PRODUCTION_FIELDS = (
    "dependencies",
    "optionalDependencies",
    "peerDependencies",
)

EXPECTED_MANIFEST: dict[str, Any] = {
    "name": "@ars-operandi/linear-mcp-runtime",
    "version": "1.0.0",
    "private": True,
    "description": "Pinned Linear-only mcp-remote runtime managed by Ars Operandi",
    "license": "MIT",
    "dependencies": {"mcp-remote": MCP_REMOTE_VERSION},
    "overrides": {"undici": UNDICI_VERSION},
    "engines": {"node": ">=22.0.0"},
}


class VerificationError(RuntimeError):
    """Stable, non-sensitive verifier failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_json(
    path: Path, *, unavailable: str, invalid: str
) -> tuple[bytes, dict[str, Any]]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise VerificationError(unavailable) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerificationError(unavailable)
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(invalid) from exc
    if not isinstance(value, dict):
        raise VerificationError(invalid)
    return raw, value


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _integrity_hex(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        raise VerificationError("runtime_integrity_invalid")
    encoded = value.removeprefix("sha512-")
    try:
        digest = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise VerificationError("runtime_integrity_invalid") from exc
    if len(digest) != hashlib.sha512().digest_size:
        raise VerificationError("runtime_integrity_invalid")
    return digest.hex()


def _verify_manifest(raw: bytes, manifest: dict[str, Any]) -> None:
    for field in _PRODUCTION_FIELDS:
        values = manifest.get(field, {})
        if not isinstance(values, dict):
            raise VerificationError("runtime_dependencies_invalid")
        for name, requirement in values.items():
            if (
                not isinstance(name, str)
                or not isinstance(requirement, str)
                or not _EXACT_VERSION_RE.fullmatch(requirement)
            ):
                raise VerificationError("runtime_dependency_unpinned")
    if manifest.get("dependencies") != {"mcp-remote": MCP_REMOTE_VERSION}:
        raise VerificationError("mcp_remote_manifest_mismatch")
    if manifest.get("overrides") != {"undici": UNDICI_VERSION}:
        raise VerificationError("undici_override_mismatch")
    if manifest != EXPECTED_MANIFEST:
        raise VerificationError("runtime_manifest_mismatch")
    if _sha256(raw) != EXPECTED_PACKAGE_SHA256:
        raise VerificationError("runtime_manifest_digest_mismatch")


def _package_name(package_path: str) -> str:
    marker = "node_modules/"
    if marker not in package_path:
        raise VerificationError("runtime_lock_invalid")
    name = package_path.rsplit(marker, 1)[1]
    if not name or (name.startswith("@") and "/" not in name):
        raise VerificationError("runtime_lock_invalid")
    return name


def _dependency_candidates(package_path: str, dependency: str) -> list[str]:
    dependency_parts = tuple(PurePosixPath(dependency).parts)
    if not dependency_parts or any(
        part in {"", ".", ".."} for part in dependency_parts
    ):
        raise VerificationError("runtime_lock_invalid")
    if package_path == "":
        return [PurePosixPath("node_modules", *dependency_parts).as_posix()]

    parts = PurePosixPath(package_path).parts
    candidates = [PurePosixPath(*parts, "node_modules", *dependency_parts).as_posix()]
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "node_modules":
            candidates.append(
                PurePosixPath(
                    *parts[:index], "node_modules", *dependency_parts
                ).as_posix()
            )
    return list(dict.fromkeys(candidates))


def _resolve_dependency(
    packages: dict[str, Any], package_path: str, dependency: str
) -> str:
    for candidate in _dependency_candidates(package_path, dependency):
        if candidate in packages:
            return candidate
    raise VerificationError("runtime_lock_incomplete")


def _verify_lock(raw: bytes, lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        lock.get("name") != EXPECTED_MANIFEST["name"]
        or lock.get("version") != EXPECTED_MANIFEST["version"]
        or lock.get("lockfileVersion") != 3
        or lock.get("requires") is not True
    ):
        raise VerificationError("runtime_lock_invalid")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise VerificationError("runtime_lock_invalid")
    expected_root = {
        "name": EXPECTED_MANIFEST["name"],
        "version": EXPECTED_MANIFEST["version"],
        "license": EXPECTED_MANIFEST["license"],
        "dependencies": EXPECTED_MANIFEST["dependencies"],
        "engines": EXPECTED_MANIFEST["engines"],
    }
    if packages.get("") != expected_root:
        raise VerificationError("runtime_lock_root_mismatch")

    for package_path, value in packages.items():
        if package_path == "":
            continue
        if not isinstance(package_path, str) or not isinstance(value, dict):
            raise VerificationError("runtime_lock_invalid")
        _package_name(package_path)
        version = value.get("version")
        resolved = value.get("resolved")
        if not isinstance(version, str) or not _EXACT_VERSION_RE.fullmatch(version):
            raise VerificationError("runtime_lock_unpinned")
        if (
            not isinstance(resolved, str)
            or not resolved.startswith("https://registry.npmjs.org/")
            or not resolved.endswith(".tgz")
        ):
            raise VerificationError("runtime_resolution_invalid")
        _integrity_hex(value.get("integrity"))
        dependencies = value.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise VerificationError("runtime_lock_invalid")
        for dependency in dependencies:
            if not isinstance(dependency, str):
                raise VerificationError("runtime_lock_invalid")
            _resolve_dependency(packages, package_path, dependency)

    remote = packages.get("node_modules/mcp-remote")
    undici = packages.get("node_modules/undici")
    if not isinstance(remote, dict) or (
        remote.get("version"),
        remote.get("resolved"),
        remote.get("integrity"),
    ) != (MCP_REMOTE_VERSION, MCP_REMOTE_RESOLVED, MCP_REMOTE_INTEGRITY):
        raise VerificationError("mcp_remote_lock_mismatch")
    if not isinstance(undici, dict) or (
        undici.get("version"),
        undici.get("resolved"),
        undici.get("integrity"),
    ) != (UNDICI_VERSION, UNDICI_RESOLVED, UNDICI_INTEGRITY):
        raise VerificationError("undici_lock_mismatch")
    if [path for path in packages if path.endswith("node_modules/undici")] != [
        "node_modules/undici"
    ]:
        raise VerificationError("undici_lock_mismatch")
    if _sha256(raw) != EXPECTED_LOCK_SHA256:
        raise VerificationError("runtime_lock_digest_mismatch")
    return packages


def _component_path(component: dict[str, Any]) -> str:
    properties = component.get("properties")
    if not isinstance(properties, list):
        raise VerificationError("sbom_component_invalid")
    matches = [
        item.get("value")
        for item in properties
        if isinstance(item, dict) and item.get("name") == "cdx:npm:package:path"
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise VerificationError("sbom_component_invalid")
    return matches[0]


def _verify_component(
    package_path: str, package: dict[str, Any], component: dict[str, Any]
) -> str:
    name = _package_name(package_path)
    version = package["version"]
    expected_ref = f"{name}@{version}"
    if (
        component.get("bom-ref") != expected_ref
        or component.get("name") != name
        or component.get("version") != version
        or component.get("type") != "library"
        or component.get("scope") != "required"
    ):
        raise VerificationError("sbom_component_mismatch")
    references = component.get("externalReferences")
    distributions = {
        item.get("url")
        for item in references or []
        if isinstance(item, dict) and item.get("type") == "distribution"
    }
    if distributions != {package["resolved"]}:
        raise VerificationError("sbom_component_mismatch")
    hashes = component.get("hashes")
    sha512_hashes = {
        item.get("content")
        for item in hashes or []
        if isinstance(item, dict) and item.get("alg") == "SHA-512"
    }
    if sha512_hashes != {_integrity_hex(package["integrity"])}:
        raise VerificationError("sbom_component_mismatch")
    return expected_ref


def _expected_graph(
    packages: dict[str, Any], component_refs: dict[str, str], root_ref: str
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for package_path, package in packages.items():
        source_ref = root_ref if package_path == "" else component_refs[package_path]
        dependencies = package.get("dependencies", {})
        graph[source_ref] = {
            component_refs[_resolve_dependency(packages, package_path, dependency)]
            for dependency in dependencies
        }
    return graph


def _verify_sbom(path: Path, packages: dict[str, Any]) -> int:
    _, sbom = _read_json(path, unavailable="sbom_unavailable", invalid="sbom_invalid")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        raise VerificationError("sbom_invalid")
    metadata = sbom.get("metadata")
    root_component = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(root_component, dict) or _component_path(root_component) != "":
        raise VerificationError("sbom_root_mismatch")
    root_ref = f"{EXPECTED_MANIFEST['name']}@{EXPECTED_MANIFEST['version']}"
    expected_purl = (
        "pkg:npm/"
        f"{quote(str(EXPECTED_MANIFEST['name']), safe='/')}"
        f"@{EXPECTED_MANIFEST['version']}"
    )
    if (
        root_component.get("bom-ref") != root_ref
        or root_component.get("version") != EXPECTED_MANIFEST["version"]
        or root_component.get("purl") != expected_purl
    ):
        raise VerificationError("sbom_root_mismatch")

    components = sbom.get("components")
    if not isinstance(components, list):
        raise VerificationError("sbom_invalid")
    components_by_path: dict[str, dict[str, Any]] = {}
    for component in components:
        if not isinstance(component, dict):
            raise VerificationError("sbom_component_invalid")
        package_path = _component_path(component)
        if not package_path or package_path in components_by_path:
            raise VerificationError("sbom_component_invalid")
        components_by_path[package_path] = component
    expected_paths = set(packages) - {""}
    if set(components_by_path) != expected_paths:
        raise VerificationError("sbom_components_mismatch")

    component_refs = {
        package_path: _verify_component(
            package_path, packages[package_path], components_by_path[package_path]
        )
        for package_path in sorted(expected_paths)
    }
    if len(set(component_refs.values())) != len(component_refs):
        raise VerificationError("sbom_component_invalid")

    dependencies = sbom.get("dependencies")
    if not isinstance(dependencies, list):
        raise VerificationError("sbom_dependencies_invalid")
    actual_graph: dict[str, set[str]] = {}
    for item in dependencies:
        if not isinstance(item, dict):
            raise VerificationError("sbom_dependencies_invalid")
        ref = item.get("ref")
        depends_on = item.get("dependsOn")
        if (
            not isinstance(ref, str)
            or ref in actual_graph
            or not isinstance(depends_on, list)
            or any(not isinstance(value, str) for value in depends_on)
            or len(set(depends_on)) != len(depends_on)
        ):
            raise VerificationError("sbom_dependencies_invalid")
        actual_graph[ref] = set(depends_on)
    if actual_graph != _expected_graph(packages, component_refs, root_ref):
        raise VerificationError("sbom_dependencies_mismatch")
    return len(components)


def _verify_audit(path: Path) -> dict[str, int]:
    _, audit = _read_json(
        path, unavailable="audit_report_unavailable", invalid="audit_report_invalid"
    )
    metadata = audit.get("metadata")
    counts = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    vulnerabilities = audit.get("vulnerabilities")
    if (
        audit.get("auditReportVersion") != 2
        or not isinstance(counts, dict)
        or not isinstance(vulnerabilities, dict)
    ):
        raise VerificationError("audit_report_invalid")
    required = ("info", "low", "moderate", "high", "critical", "total")
    if any(
        not isinstance(counts.get(level), int)
        or isinstance(counts.get(level), bool)
        or counts[level] < 0
        for level in required
    ):
        raise VerificationError("audit_report_invalid")
    if (
        counts["high"]
        or counts["critical"]
        or any(
            isinstance(value, dict) and value.get("severity") in {"high", "critical"}
            for value in vulnerabilities.values()
        )
    ):
        raise VerificationError("audit_high_or_critical")
    return {level: counts[level] for level in required}


def verify(
    runtime_root: Path,
    *,
    sbom_path: Path | None,
    audit_path: Path | None,
) -> dict[str, Any]:
    if sbom_path is None:
        raise VerificationError("sbom_evidence_required")
    if audit_path is None:
        raise VerificationError("audit_evidence_required")
    package_raw, manifest = _read_json(
        runtime_root / "package.json",
        unavailable="runtime_manifest_unavailable",
        invalid="runtime_manifest_invalid",
    )
    lock_raw, lock = _read_json(
        runtime_root / "package-lock.json",
        unavailable="runtime_lock_unavailable",
        invalid="runtime_lock_invalid",
    )
    _verify_manifest(package_raw, manifest)
    packages = _verify_lock(lock_raw, lock)
    return {
        "status": "verified",
        "mcp_remote": MCP_REMOTE_VERSION,
        "undici": UNDICI_VERSION,
        "lock_sha256": EXPECTED_LOCK_SHA256,
        "package_count": len(packages) - 1,
        "sbom_components": _verify_sbom(sbom_path, packages),
        "audit": _verify_audit(audit_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(
            args.runtime_root,
            sbom_path=args.sbom,
            audit_path=args.audit_report,
        )
    except VerificationError as exc:
        print(
            json.dumps({"status": "error", "code": exc.code}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
