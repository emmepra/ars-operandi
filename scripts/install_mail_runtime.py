#!/usr/bin/env python3
"""Dry-run-first installer for the canonical Ars mail skill and MCP runtime."""

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


MCP_ALIAS = "ars-mail"
SKILL_NAME = "mailctl-email-access"
MARKER_NAME = ".ars-operandi-install.json"
MARKER_ID = "ars-operandi.mailctl-email-access"
MARKER_VERSION = 1


class InstallError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _skills_root(value: str | None) -> Path:
    return (
        Path(value).expanduser().resolve()
        if value
        else (Path.home() / ".codex" / "skills").resolve()
    )


def _included(path: Path) -> bool:
    return (
        path.name not in {MARKER_NAME, ".DS_Store"}
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (path for path in root.rglob("*") if path.is_file() and _included(path)),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _run_codex(
    executable: str, arguments: list[str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [executable, *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            "codex_cli_unavailable", "Codex CLI configuration access was unavailable."
        ) from exc


def _config_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    transport = payload.get("transport")
    if not isinstance(transport, Mapping):
        raise InstallError("mcp_config_invalid", "The MCP configuration is invalid.")
    args = transport.get("args")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise InstallError("mcp_config_invalid", "The MCP arguments are invalid.")
    if transport.get("env") not in (None, {}) or transport.get("env_vars", []) not in (
        None,
        [],
    ):
        raise InstallError(
            "mcp_config_conflict", "The mail MCP must not contain environment data."
        )
    return {
        "name": payload.get("name"),
        "enabled": payload.get("enabled"),
        "transport": {
            "type": transport.get("type"),
            "command": transport.get("command"),
            "args": args,
            "env": None,
            "env_vars": [],
            "cwd": transport.get("cwd"),
        },
    }


def _get_mcp(executable: str) -> dict[str, Any] | None:
    completed = _run_codex(executable, ["mcp", "get", MCP_ALIAS, "--json"])
    if completed.returncode != 0:
        if "No MCP server named" in completed.stderr:
            return None
        raise InstallError(
            "codex_config_unavailable", "Codex could not inspect the mail MCP."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InstallError(
            "mcp_config_invalid", "Codex returned invalid MCP JSON."
        ) from exc
    if not isinstance(payload, Mapping):
        raise InstallError("mcp_config_invalid", "Codex returned invalid MCP JSON.")
    return _config_view(payload)


def _desired_config(
    runtime_repo: Path, project_index: Path, config_root: Path
) -> dict[str, Any]:
    return {
        "name": MCP_ALIAS,
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--offline",
                "--project",
                str(runtime_repo),
                "mailctl",
                "serve",
                "--project-index",
                str(project_index),
                "--config-root",
                str(config_root),
            ],
            "env": None,
            "env_vars": [],
            "cwd": None,
        },
    }


def _load_marker(destination: Path) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    marker = destination / MARKER_NAME
    if destination.is_symlink() or not destination.is_dir() or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            "unmanaged_skill_conflict", "The installed mail skill marker is invalid."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("installer") != MARKER_ID
        or payload.get("version") != MARKER_VERSION
        or not isinstance(payload.get("source_hash"), str)
        or not isinstance(payload.get("mcp_config"), Mapping)
    ):
        raise InstallError(
            "unmanaged_skill_conflict", "The installed mail skill marker is invalid."
        )
    if _tree_hash(destination) != payload["source_hash"]:
        raise InstallError(
            "managed_skill_drift", "The managed mail skill has local drift."
        )
    payload["mcp_config"] = _config_view(payload["mcp_config"])
    return payload


def _add_mcp(executable: str, config: Mapping[str, Any]) -> None:
    transport = config["transport"]
    completed = _run_codex(
        executable,
        [
            "mcp",
            "add",
            MCP_ALIAS,
            "--",
            str(transport["command"]),
            *transport["args"],
        ],
    )
    if completed.returncode != 0:
        raise InstallError("mcp_apply_failed", "Codex could not register the mail MCP.")


def _remove_mcp(executable: str) -> None:
    if _run_codex(executable, ["mcp", "remove", MCP_ALIAS]).returncode != 0:
        raise InstallError("mcp_apply_failed", "Codex could not remove the mail MCP.")


def _replace_mcp(
    executable: str,
    current: Mapping[str, Any] | None,
    desired: Mapping[str, Any],
) -> None:
    if current == desired:
        return
    if current is None:
        _add_mcp(executable, desired)
        return
    _remove_mcp(executable)
    try:
        _add_mcp(executable, desired)
    except InstallError:
        _add_mcp(executable, current)
        raise


def _validate_runtime(runtime_repo: Path, project_index: Path) -> None:
    if runtime_repo != _repo_root():
        raise InstallError(
            "runtime_owner_mismatch",
            "The canonical mail runtime must be this Ars Operandi checkout.",
        )
    if (
        not (runtime_repo / "pyproject.toml").is_file()
        or not (runtime_repo / "src" / "ars_operandi_mail").is_dir()
    ):
        raise InstallError("runtime_unavailable", "The Ars mail runtime is incomplete.")
    if not project_index.is_file():
        raise InstallError(
            "manifest_unavailable", "The explicit Project Index is unavailable."
        )


def _prewarm(runtime_repo: Path) -> None:
    try:
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--offline",
                "--project",
                str(runtime_repo),
                "mailctl",
                "--help",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(
            "runtime_unavailable", "The mail runtime could not start."
        ) from exc
    if completed.returncode != 0:
        raise InstallError("runtime_unavailable", "The mail runtime could not start.")


def _copy_skill(
    source: Path,
    destination: Path,
    marker: dict[str, Any],
    *,
    replace_unmanaged: bool,
) -> tuple[Path | None, Path | None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(
        tempfile.mkdtemp(prefix=".ars-mail-stage-", dir=destination.parent)
    )
    staged = stage_root / SKILL_NAME
    prior: Path | None = None
    durable_backup: Path | None = None
    try:
        shutil.copytree(
            source,
            staged,
            ignore=shutil.ignore_patterns(
                ".DS_Store", "__pycache__", "*.pyc", MARKER_NAME
            ),
        )
        if destination.exists():
            prior = destination.parent / f".ars-mail-prior-{uuid.uuid4().hex}"
            os.replace(destination, prior)
            if replace_unmanaged:
                durable_backup = (
                    destination.parent
                    / f"mailctl-email-access.backup-{uuid.uuid4().hex}"
                )
                os.replace(prior, durable_backup)
                prior = None
                marker["preexisting_backup"] = str(durable_backup)
        (staged / MARKER_NAME).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(staged, destination)
        return prior, durable_backup
    except Exception:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        restore = prior or durable_backup
        if restore is not None and restore.exists():
            os.replace(restore, destination)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _restore_skill(destination: Path, prior: Path | None, backup: Path | None) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    restore = prior or backup
    if restore is not None and restore.exists():
        os.replace(restore, destination)


def _install(args: argparse.Namespace) -> dict[str, Any]:
    runtime_repo = args.runtime_repo.expanduser().resolve()
    project_index = args.project_index.expanduser().resolve()
    config_root = args.config_root.expanduser().resolve()
    _validate_runtime(runtime_repo, project_index)
    source = runtime_repo / "skills" / SKILL_NAME
    destination = _skills_root(args.skills_root) / SKILL_NAME
    installed = _load_marker(destination)
    unmanaged = destination.exists() and installed is None
    if unmanaged and not args.replace_existing_skill:
        raise InstallError(
            "unmanaged_skill_conflict",
            "An existing mail skill requires explicit --replace-existing-skill.",
        )
    desired = _desired_config(runtime_repo, project_index, config_root)
    current = _get_mcp(args.codex_executable)
    if current is not None and installed is None:
        raise InstallError(
            "unmanaged_mcp_conflict", "An unmanaged ars-mail MCP already exists."
        )
    previous = installed["mcp_config"] if installed else None
    if current is not None and current != previous and current != desired:
        raise InstallError("mcp_config_conflict", "The managed mail MCP has drift.")
    source_hash = _tree_hash(source)
    skill_change = (
        unmanaged
        or installed is None
        or installed["source_hash"] != source_hash
        or previous != desired
    )
    mcp_change = current != desired
    actions = [
        name
        for name, needed in (
            ("install-or-update-skill", skill_change),
            ("register-or-update-ars-mail-mcp", mcp_change),
        )
        if needed
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
        return {"ok": True, "mode": "apply", "status": "no-op", "alias": MCP_ALIAS}

    marker = {
        "installer": MARKER_ID,
        "version": MARKER_VERSION,
        "source_hash": source_hash,
        "mcp_config": desired,
        "preexisting_backup": installed.get("preexisting_backup")
        if installed
        else None,
    }
    prior: Path | None = None
    backup: Path | None = None
    if skill_change:
        prior, backup = _copy_skill(
            source,
            destination,
            marker,
            replace_unmanaged=unmanaged,
        )
    try:
        _prewarm(runtime_repo)
        if mcp_change:
            _replace_mcp(args.codex_executable, current, desired)
    except InstallError:
        if skill_change:
            _restore_skill(destination, prior, backup)
        raise
    if prior is not None:
        shutil.rmtree(prior, ignore_errors=True)
    return {
        "ok": True,
        "mode": "apply",
        "status": "installed" if installed is None else "updated",
        "alias": MCP_ALIAS,
    }


def _smoke(args: argparse.Namespace) -> dict[str, Any]:
    destination = _skills_root(args.skills_root) / SKILL_NAME
    installed = _load_marker(destination)
    if installed is None:
        raise InstallError(
            "skill_not_installed", "The managed mail skill is unavailable."
        )
    current = _get_mcp(args.codex_executable)
    if current != installed["mcp_config"]:
        raise InstallError(
            "mcp_config_conflict", "Installed mail MCP state does not match."
        )
    runtime_repo = Path(current["transport"]["args"][3]).resolve()
    _prewarm(runtime_repo)
    return {
        "ok": True,
        "mode": "read-only",
        "status": "config-verified",
        "alias": MCP_ALIAS,
        "checks": ["managed-skill", "single-mcp-alias", "offline-runtime"],
    }


def _rollback(args: argparse.Namespace) -> dict[str, Any]:
    destination = _skills_root(args.skills_root) / SKILL_NAME
    installed = _load_marker(destination)
    current = _get_mcp(args.codex_executable)
    if installed is None:
        if current is not None:
            raise InstallError(
                "unmanaged_mcp_conflict", "An unmanaged ars-mail MCP exists."
            )
        return {
            "ok": True,
            "mode": "apply" if args.apply else "dry-run",
            "status": "no-op",
            "alias": MCP_ALIAS,
        }
    if current is not None and current != installed["mcp_config"]:
        raise InstallError(
            "mcp_config_conflict", "Managed mail MCP drift blocks rollback."
        )
    actions = (["remove-managed-mcp"] if current is not None else []) + [
        "remove-managed-skill"
    ]
    if installed.get("preexisting_backup"):
        actions.append("restore-preexisting-skill")
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
    removed = destination.parent / f".ars-mail-remove-{uuid.uuid4().hex}"
    try:
        os.replace(destination, removed)
    except OSError as exc:
        if current is not None:
            try:
                _add_mcp(args.codex_executable, installed["mcp_config"])
            except InstallError as restore_exc:
                raise InstallError(
                    "mcp_rollback_failed",
                    "Mail rollback failed and the prior MCP configuration could not be restored.",
                ) from restore_exc
        raise InstallError(
            "skill_remove_failed",
            "The managed mail skill could not be removed safely.",
        ) from exc
    backup_value = installed.get("preexisting_backup")
    backup = Path(backup_value) if isinstance(backup_value, str) else None
    try:
        if backup is not None:
            if not backup.is_dir():
                raise InstallError(
                    "rollback_failed", "The preexisting skill backup is missing."
                )
            os.replace(backup, destination)
        shutil.rmtree(removed)
    except Exception:
        if destination.exists() and backup is not None:
            os.replace(destination, backup)
        os.replace(removed, destination)
        if current is not None:
            _add_mcp(args.codex_executable, installed["mcp_config"])
        raise
    return {"ok": True, "mode": "apply", "status": "removed", "alias": MCP_ALIAS}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--runtime-repo", type=Path, required=True)
    install.add_argument("--project-index", type=Path, required=True)
    install.add_argument("--config-root", type=Path, required=True)
    install.add_argument("--skills-root")
    install.add_argument("--codex-executable", default="codex")
    install.add_argument("--replace-existing-skill", action="store_true")
    install.add_argument("--apply", action="store_true")
    for name in ("smoke", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("--skills-root")
        command.add_argument("--codex-executable", default="codex")
        if name == "rollback":
            command.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            payload = _install(args)
        elif args.command == "smoke":
            payload = _smoke(args)
        else:
            payload = _rollback(args)
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
