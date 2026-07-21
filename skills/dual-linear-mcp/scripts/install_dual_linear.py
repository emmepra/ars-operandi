#!/usr/bin/env python3
"""Idempotently install or remove the managed dual Linear Codex adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping


MCP_ALIAS = "dual-linear"
SKILL_NAME = "dual-linear-mcp"
MARKER_NAME = ".ars-operandi-install.json"
MARKER_ID = "ars-operandi.dual-linear-mcp"
MARKER_VERSION = 1
AUTH_ENV_KEYS = frozenset(
    {
        "OP_ACCOUNT",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_SERVICE_ACCOUNT_TOKEN",
        "OP_SESSION",
    }
)


class InstallError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _skills_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".agents" / "skills").resolve()


def _source_skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _included_file(path: Path) -> bool:
    return (
        path.name != MARKER_NAME
        and path.name != ".DS_Store"
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    )


def _source_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file() and _included_file(path.relative_to(root))
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _source_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {MARKER_NAME, ".DS_Store", "__pycache__"} or name.endswith(".pyc")
    }


def _subprocess_env() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in AUTH_ENV_KEYS and not key.startswith("OP_SESSION_")
    }
    environment["CODEX_SKIP_1P_SIGNIN"] = "1"
    return environment


def _run_codex(codex: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [codex, *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env=_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            "codex_cli_unavailable",
            "Codex CLI configuration access was unavailable.",
        ) from exc


def _config_view(value: Mapping[str, Any]) -> dict[str, Any]:
    transport = value.get("transport")
    if not isinstance(transport, Mapping):
        raise InstallError(
            "mcp_config_invalid",
            "The existing dual-linear MCP configuration is not a STDIO mapping.",
        )
    env = transport.get("env")
    env_vars = transport.get("env_vars", [])
    if env not in (None, {}) or env_vars not in (None, []):
        raise InstallError(
            "mcp_config_conflict",
            "The existing dual-linear MCP configuration contains environment data.",
        )
    raw_args = transport.get("args")
    if not isinstance(raw_args, list) or not all(
        isinstance(item, str) for item in raw_args
    ):
        raise InstallError(
            "mcp_config_invalid",
            "The existing dual-linear MCP arguments are invalid.",
        )
    return {
        "name": value.get("name"),
        "enabled": value.get("enabled"),
        "transport": {
            "type": transport.get("type"),
            "command": transport.get("command"),
            "args": raw_args,
            "env": None,
            "env_vars": [],
            "cwd": transport.get("cwd"),
        },
    }


def _get_mcp_config(codex: str) -> dict[str, Any] | None:
    completed = _run_codex(codex, ["mcp", "get", MCP_ALIAS, "--json"])
    if completed.returncode != 0:
        if "No MCP server named" in completed.stderr:
            return None
        raise InstallError(
            "codex_config_unavailable",
            "Codex could not inspect the dual-linear MCP configuration.",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(
            "mcp_config_invalid",
            "Codex returned an invalid dual-linear MCP configuration.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise InstallError(
            "mcp_config_invalid",
            "Codex returned an invalid dual-linear MCP configuration.",
        )
    return _config_view(payload)


def _desired_config(
    *,
    destination: Path,
    manifest: Path,
    reference_template: str,
    account: str,
    auth_scheme: str,
) -> dict[str, Any]:
    adapter = destination / "scripts" / "dual_linear_mcp.py"
    return {
        "name": MCP_ALIAS,
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--offline",
                "--script",
                str(adapter),
                "serve",
                "--manifest",
                str(manifest),
                "--op-reference-template",
                reference_template,
                "--op-auth-mode",
                "direct",
                "--op-account",
                account,
                "--auth-scheme",
                auth_scheme,
            ],
            "env": None,
            "env_vars": [],
            "cwd": None,
        },
    }


def _prewarm_adapter(
    uv: str,
    destination: Path,
    manifest: Path,
    *,
    offline: bool,
) -> None:
    arguments = [uv, "run"]
    if offline:
        arguments.append("--offline")
    arguments.extend(
        [
            "--script",
            str(destination / "scripts" / "dual_linear_mcp.py"),
            "validate-manifest",
            "--manifest",
            str(manifest),
        ]
    )
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            env=_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            "adapter_runtime_unavailable",
            "The adapter runtime could not be prepared without exposing details.",
        ) from exc
    if completed.returncode != 0:
        raise InstallError(
            "adapter_runtime_unavailable",
            "The adapter runtime could not be prepared without exposing details.",
        )


def _config_argument(config: Mapping[str, Any], flag: str) -> str:
    arguments = config["transport"]["args"]
    try:
        index = arguments.index(flag)
        value = arguments[index + 1]
    except (ValueError, IndexError) as exc:
        raise InstallError(
            "mcp_config_invalid",
            "The managed dual-linear MCP configuration is incomplete.",
        ) from exc
    if not isinstance(value, str) or not value:
        raise InstallError(
            "mcp_config_invalid",
            "The managed dual-linear MCP configuration is incomplete.",
        )
    return value


def _marker_payload(source_hash: str, config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "installer": MARKER_ID,
        "version": MARKER_VERSION,
        "source_hash": source_hash,
        "mcp_alias": MCP_ALIAS,
        "mcp_config": dict(config),
    }


def _load_marker(destination: Path) -> dict[str, Any] | None:
    marker_path = destination / MARKER_NAME
    if not destination.exists():
        return None
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or not marker_path.is_file()
    ):
        raise InstallError(
            "unmanaged_skill_conflict",
            "The dual-linear-mcp skill destination exists but is not installer-managed.",
        )
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            "unmanaged_skill_conflict",
            "The dual-linear-mcp installer marker is invalid.",
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("installer") != MARKER_ID
        or payload.get("version") != MARKER_VERSION
        or payload.get("mcp_alias") != MCP_ALIAS
        or not isinstance(payload.get("source_hash"), str)
        or not isinstance(payload.get("mcp_config"), Mapping)
    ):
        raise InstallError(
            "unmanaged_skill_conflict",
            "The dual-linear-mcp installer marker is invalid.",
        )
    if _tree_hash(destination) != payload["source_hash"]:
        raise InstallError(
            "managed_skill_drift",
            "The managed dual-linear-mcp skill has local drift; refusing to overwrite it.",
        )
    payload["mcp_config"] = _config_view(payload["mcp_config"])
    return payload


def _add_mcp(codex: str, config: Mapping[str, Any]) -> None:
    transport = config["transport"]
    completed = _run_codex(
        codex,
        [
            "mcp",
            "add",
            MCP_ALIAS,
            "--",
            transport["command"],
            *transport["args"],
        ],
    )
    if completed.returncode != 0:
        raise InstallError(
            "mcp_apply_failed",
            "Codex could not register the dual-linear MCP configuration.",
        )


def _remove_mcp(codex: str) -> None:
    completed = _run_codex(codex, ["mcp", "remove", MCP_ALIAS])
    if completed.returncode != 0:
        raise InstallError(
            "mcp_apply_failed",
            "Codex could not remove the dual-linear MCP configuration.",
        )


def _replace_skill(
    source: Path,
    destination: Path,
    marker: Mapping[str, Any],
) -> Path | None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".dual-linear-stage-", dir=destination.parent)
    )
    staged = staging_root / SKILL_NAME
    backup: Path | None = None
    try:
        shutil.copytree(source, staged, ignore=_copy_ignore)
        (staged / MARKER_NAME).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            backup = destination.parent / f".dual-linear-backup-{uuid.uuid4().hex}"
            os.replace(destination, backup)
        os.replace(staged, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return backup


def _restore_skill(destination: Path, backup: Path | None) -> None:
    failed = destination.parent / f".dual-linear-failed-{uuid.uuid4().hex}"
    if destination.exists():
        os.replace(destination, failed)
    if backup is not None and backup.exists():
        os.replace(backup, destination)
    shutil.rmtree(failed, ignore_errors=True)


def _apply_mcp_change(
    codex: str,
    current: Mapping[str, Any] | None,
    desired: Mapping[str, Any],
) -> None:
    if current == desired:
        return
    if current is None:
        _add_mcp(codex, desired)
        return
    _remove_mcp(codex)
    try:
        _add_mcp(codex, desired)
    except InstallError as exc:
        try:
            _add_mcp(codex, current)
        except InstallError as restore_exc:
            raise InstallError(
                "mcp_rollback_failed",
                "The MCP update failed and the prior managed configuration could not be restored.",
            ) from restore_exc
        raise exc


def _install(args: argparse.Namespace) -> dict[str, Any]:
    source = _source_skill_root()
    destination = _skills_root(args.skills_root) / SKILL_NAME
    manifest = args.manifest.expanduser().resolve()
    if not manifest.is_file():
        raise InstallError(
            "manifest_unavailable",
            "The explicit manifest path does not identify a readable file.",
        )
    if "{profile}" not in args.op_reference_template:
        raise InstallError(
            "invalid_configuration",
            "The 1Password reference template must contain {profile}.",
        )
    if not args.op_account.strip():
        raise InstallError(
            "invalid_configuration",
            "An explicit non-secret 1Password account selector is required.",
        )
    source_hash = _tree_hash(source)
    desired = _desired_config(
        destination=destination,
        manifest=manifest,
        reference_template=args.op_reference_template,
        account=args.op_account.strip(),
        auth_scheme=args.auth_scheme,
    )
    installed = _load_marker(destination)
    current = _get_mcp_config(args.codex_executable)
    if installed is None and current is not None:
        raise InstallError(
            "unmanaged_mcp_conflict",
            "A dual-linear MCP configuration exists without an installer-managed skill.",
        )
    previous_config = installed["mcp_config"] if installed is not None else None
    if current is not None and current != desired and current != previous_config:
        raise InstallError(
            "mcp_config_conflict",
            "The existing dual-linear MCP configuration differs from managed state.",
        )
    skill_change = (
        installed is None
        or installed["source_hash"] != source_hash
        or previous_config != desired
    )
    mcp_change = current != desired
    actions = [
        action
        for action, required in (
            ("install-or-update-skill", skill_change),
            ("register-or-update-mcp", mcp_change),
        )
        if required
    ]
    if not args.apply:
        return {
            "ok": True,
            "mode": "dry-run",
            "status": "planned" if actions else "no-op",
            "alias": MCP_ALIAS,
            "actions": actions,
        }
    if not actions:
        return {
            "ok": True,
            "mode": "apply",
            "status": "no-op",
            "alias": MCP_ALIAS,
        }
    marker = _marker_payload(source_hash, desired)
    backup: Path | None = None
    if skill_change:
        try:
            backup = _replace_skill(source, destination, marker)
        except OSError as exc:
            raise InstallError(
                "skill_apply_failed",
                "The managed skill could not be installed atomically.",
            ) from exc
        try:
            _prewarm_adapter(
                args.uv_executable,
                destination,
                manifest,
                offline=False,
            )
        except InstallError:
            _restore_skill(destination, backup)
            raise
    try:
        if mcp_change:
            _apply_mcp_change(args.codex_executable, current, desired)
    except InstallError:
        if skill_change:
            _restore_skill(destination, backup)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return {
        "ok": True,
        "mode": "apply",
        "status": "installed" if installed is None else "updated",
        "alias": MCP_ALIAS,
    }


def _rollback(args: argparse.Namespace) -> dict[str, Any]:
    destination = _skills_root(args.skills_root) / SKILL_NAME
    installed = _load_marker(destination)
    current = _get_mcp_config(args.codex_executable)
    if installed is None:
        if current is not None:
            raise InstallError(
                "unmanaged_mcp_conflict",
                "A dual-linear MCP configuration exists without an installer-managed skill.",
            )
        return {
            "ok": True,
            "mode": "apply" if args.apply else "dry-run",
            "status": "no-op",
            "alias": MCP_ALIAS,
        }
    managed_config = installed["mcp_config"]
    if current is not None and current != managed_config:
        raise InstallError(
            "mcp_config_conflict",
            "The dual-linear MCP configuration has drift; rollback refused.",
        )
    actions = ["remove-managed-skill"]
    if current is not None:
        actions.insert(0, "remove-managed-mcp")
    if not args.apply:
        return {
            "ok": True,
            "mode": "dry-run",
            "status": "planned",
            "alias": MCP_ALIAS,
            "actions": actions,
        }
    if current is not None:
        _remove_mcp(args.codex_executable)
    backup = destination.parent / f".dual-linear-remove-{uuid.uuid4().hex}"
    try:
        os.replace(destination, backup)
    except OSError as exc:
        if current is not None:
            _add_mcp(args.codex_executable, managed_config)
        raise InstallError(
            "skill_remove_failed",
            "The managed skill could not be removed safely.",
        ) from exc
    shutil.rmtree(backup)
    return {
        "ok": True,
        "mode": "apply",
        "status": "removed",
        "alias": MCP_ALIAS,
    }


def _smoke(args: argparse.Namespace) -> dict[str, Any]:
    destination = _skills_root(args.skills_root) / SKILL_NAME
    installed = _load_marker(destination)
    if installed is None:
        raise InstallError(
            "skill_not_installed",
            "The installer-managed dual-linear-mcp skill was not found.",
        )
    for relative in (
        "SKILL.md",
        "scripts/dual_linear_mcp.py",
        "scripts/smoke_dual_linear_mcp.py",
    ):
        if not (destination / relative).is_file():
            raise InstallError(
                "managed_skill_drift",
                "The managed dual-linear-mcp skill is incomplete.",
            )
    current = _get_mcp_config(args.codex_executable)
    if current != installed["mcp_config"]:
        raise InstallError(
            "mcp_config_conflict",
            "The installed dual-linear MCP configuration does not match managed state.",
        )
    manifest = Path(_config_argument(current, "--manifest"))
    _prewarm_adapter(
        args.uv_executable,
        destination,
        manifest,
        offline=True,
    )
    return {
        "ok": True,
        "mode": "read-only",
        "status": "config-verified",
        "alias": MCP_ALIAS,
        "checks": ["managed-skill", "codex-mcp-get", "offline-runtime"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install")
    install.add_argument("--manifest", type=Path, required=True)
    install.add_argument("--op-reference-template", required=True)
    install.add_argument("--op-account", required=True)
    install.add_argument(
        "--auth-scheme", choices=("api-key", "bearer"), default="api-key"
    )
    install.add_argument("--skills-root")
    install.add_argument("--codex-executable", default="codex")
    install.add_argument("--uv-executable", default="uv")
    install.add_argument("--apply", action="store_true")

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--skills-root")
    rollback.add_argument("--codex-executable", default="codex")
    rollback.add_argument("--apply", action="store_true")

    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--skills-root")
    smoke.add_argument("--codex-executable", default="codex")
    smoke.add_argument("--uv-executable", default="uv")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            payload = _install(args)
        elif args.command == "rollback":
            payload = _rollback(args)
        else:
            payload = _smoke(args)
        print(json.dumps(payload, sort_keys=True))
        return 0
    except InstallError as exc:
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
                        "code": "installer_internal_error",
                        "message": "The installer failed without exposing runtime details.",
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
