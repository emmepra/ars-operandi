#!/usr/bin/env python3
"""Dry-run-first installer and exact-target Codex config planner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any, Mapping


SKILL_NAME = "official-linear-mcp-bridge"
MARKER_NAME = ".ars-operandi-install.json"
MARKER_ID = "ars-operandi.official-linear-mcp-bridge"
MARKER_VERSION = 2
MARKER_MODE = 0o600
BLOCK_BEGIN = "# BEGIN ars-operandi official-linear-mcp-bridge connections v2"
BLOCK_END = "# END ars-operandi official-linear-mcp-bridge connections v2"
RUNTIME_TIMEOUT_SECONDS = 120.0
_ALIAS_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_ACCOUNT_PATTERN = re.compile(
    r"(?=.{3,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_REFERENCE_SEGMENT_PATTERN = re.compile(
    r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2}){1,128}\Z"
)
ONEPASSWORD_AUTH_ENV_KEYS = frozenset(
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


def _valid_account(value: str) -> bool:
    return bool(_ACCOUNT_PATTERN.fullmatch(value))


def _valid_reference(value: str) -> bool:
    if len(value) > 512 or not value.startswith("op://"):
        return False
    segments = value[5:].split("/")
    return len(segments) in {3, 4} and all(
        _REFERENCE_SEGMENT_PATTERN.fullmatch(segment) for segment in segments
    )


def _parse_connections(values: list[str]) -> tuple[tuple[str, str], ...]:
    connections: list[tuple[str, str]] = []
    for value in values:
        alias, separator, reference = value.partition("=")
        if not separator or not _ALIAS_PATTERN.fullmatch(alias):
            raise InstallError(
                "invalid_connection",
                "Each connection requires a valid lowercase ALIAS=OP_REFERENCE value.",
            )
        if not _valid_reference(reference):
            raise InstallError(
                "invalid_connection",
                "Each connection requires a valid static 1Password reference.",
            )
        connections.append((alias, reference))
    aliases = [alias for alias, _reference in connections]
    references = [reference for _alias, reference in connections]
    if (
        not connections
        or len(set(aliases)) != len(aliases)
        or len(set(references)) != len(references)
    ):
        raise InstallError(
            "invalid_connection",
            "Connection aliases and references must be present and unique.",
        )
    return tuple(connections)


def _clean_runtime_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in ONEPASSWORD_AUTH_ENV_KEYS and not key.startswith("OP_SESSION")
    }


def _skills_root(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".agents" / "skills").resolve()


def _config_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".codex" / "config.toml").resolve()


def _source_skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _included_file(path: Path) -> bool:
    return (
        path.name not in {MARKER_NAME, ".DS_Store"}
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
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {MARKER_NAME, ".DS_Store", "__pycache__"} or name.endswith(".pyc")
    }


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _managed_block(
    destination: Path,
    connections: tuple[tuple[str, str], ...],
    account: str,
) -> bytes:
    adapter = destination / "scripts" / "official_linear_mcp_bridge.py"
    lines = [BLOCK_BEGIN]
    for alias, reference in connections:
        arguments = [
            "run",
            "--offline",
            "--script",
            str(adapter),
            "serve",
            "--op-reference",
            reference,
            "--op-account",
            account,
        ]
        rendered_arguments = ", ".join(_toml_string(value) for value in arguments)
        lines.extend(
            [
                f"[mcp_servers.{alias}]",
                'command = "uv"',
                f"args = [{rendered_arguments}]",
                "enabled = true",
                'default_tools_approval_mode = "writes"',
                "startup_timeout_sec = 60.0",
                "",
            ]
        )
    lines.append(BLOCK_END)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_config(path: Path) -> tuple[bytes, int, Mapping[str, Any]]:
    if not path.exists():
        return b"", 0o600, {}
    if path.is_symlink() or not path.is_file():
        raise InstallError(
            "config_unavailable", "The Codex config path is not a regular file."
        )
    data = path.read_bytes()
    try:
        decoded = data.decode("utf-8")
        parsed = tomllib.loads(decoded) if decoded.strip() else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(
            "config_invalid", "The Codex config is not valid UTF-8 TOML."
        ) from exc
    return data, stat.S_IMODE(path.stat().st_mode), parsed


def _mcp_servers(parsed: Mapping[str, Any]) -> Mapping[str, Any]:
    value = parsed.get("mcp_servers", {})
    if not isinstance(value, Mapping):
        raise InstallError(
            "config_invalid", "The Codex MCP server table is not a mapping."
        )
    return value


def _assert_no_alias_conflict(
    parsed: Mapping[str, Any], aliases: tuple[str, ...]
) -> None:
    servers = _mcp_servers(parsed)
    if any(alias in servers for alias in aliases):
        raise InstallError(
            "connection_alias_conflict",
            "A requested MCP connection alias already exists outside managed state.",
        )


def _managed_segment(prefix: bytes, block: bytes) -> bytes:
    separator = b"" if not prefix or prefix.endswith(b"\n") else b"\n"
    return separator + block


def _split_exact_segment(data: bytes, segment: bytes) -> tuple[bytes, bytes]:
    if data.count(segment) != 1:
        raise InstallError(
            "managed_config_drift",
            "The managed MCP connection config has drift; refusing mutation.",
        )
    index = data.index(segment)
    return data[:index], data[index + len(segment) :]


def _paused_managed_segment(segment: bytes, connection_count: int) -> bytes:
    enabled_line = b"\nenabled = true\n"
    paused_line = b"\nenabled = false\n"
    if (
        connection_count <= 0
        or segment.count(enabled_line) != connection_count
        or segment.count(b"enabled = true") != connection_count
        or paused_line in segment
    ):
        raise InstallError(
            "managed_config_drift",
            "The managed MCP connection config cannot be resumed safely.",
        )
    return segment.replace(enabled_line, paused_line)


def _marker_payload(
    source_hash: str,
    segment: bytes,
    connections: tuple[tuple[str, str], ...],
    account: str,
) -> dict[str, Any]:
    return {
        "installer": MARKER_ID,
        "version": MARKER_VERSION,
        "source_hash": source_hash,
        "connections": [
            {"alias": alias, "reference": reference} for alias, reference in connections
        ],
        "op_account": account,
        "config_segment": segment.decode("utf-8"),
        "config_segment_sha256": hashlib.sha256(segment).hexdigest(),
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
            "The skill destination exists but is not installer-managed.",
        )
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            "unmanaged_skill_conflict", "The skill installer marker is invalid."
        ) from exc
    segment = payload.get("config_segment") if isinstance(payload, dict) else None
    raw_connections = payload.get("connections") if isinstance(payload, dict) else None
    try:
        connections = _parse_connections(
            [f"{item['alias']}={item['reference']}" for item in raw_connections]
        )
    except (InstallError, KeyError, TypeError):
        connections = ()
    if (
        not isinstance(payload, dict)
        or payload.get("installer") != MARKER_ID
        or payload.get("version") != MARKER_VERSION
        or not isinstance(payload.get("source_hash"), str)
        or not connections
        or not isinstance(payload.get("op_account"), str)
        or not _valid_account(payload["op_account"])
        or not isinstance(segment, str)
        or payload.get("config_segment_sha256")
        != hashlib.sha256(segment.encode("utf-8")).hexdigest()
    ):
        raise InstallError(
            "unmanaged_skill_conflict", "The skill installer marker is invalid."
        )
    if _tree_hash(destination) != payload["source_hash"]:
        raise InstallError(
            "managed_skill_drift",
            "The managed skill has local drift; refusing mutation.",
        )
    payload["normalized_connections"] = connections
    return payload


def _stage_skill(
    source: Path,
    destination: Path,
    marker: Mapping[str, Any],
) -> tuple[Path, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".official-linear-stage-", dir=destination.parent)
    )
    staged = staging_root / SKILL_NAME
    try:
        shutil.copytree(source, staged, ignore=_copy_ignore)
        marker_path = staged / MARKER_NAME
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_path.chmod(MARKER_MODE)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return staging_root, staged


def _install_staged_skill(staged: Path, destination: Path) -> Path | None:
    backup: Path | None = None
    if destination.exists():
        (destination / MARKER_NAME).chmod(MARKER_MODE)
        backup = destination.parent / f".official-linear-backup-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    return backup


def _restore_skill(destination: Path, backup: Path | None) -> None:
    failed: Path | None = None
    if destination.exists():
        failed = destination.parent / f".official-linear-failed-{uuid.uuid4().hex}"
        os.replace(destination, failed)
    if backup is not None and backup.exists():
        os.replace(backup, destination)
        (destination / MARKER_NAME).chmod(MARKER_MODE)
    if failed is not None:
        shutil.rmtree(failed, ignore_errors=True)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_runtime_step(adapter: Path, *, offline: bool) -> None:
    command = ["uv", "run"]
    if offline:
        command.append("--offline")
    command.extend(["--script", str(adapter), "--help"])
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=RUNTIME_TIMEOUT_SECONDS,
            env=_clean_runtime_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        code = "runtime_offline_failed" if offline else "runtime_prepare_failed"
        raise InstallError(
            code, "The credential-free bridge runtime check failed closed."
        ) from exc
    if completed.returncode != 0:
        code = "runtime_offline_failed" if offline else "runtime_prepare_failed"
        raise InstallError(
            code, "The credential-free bridge runtime check failed closed."
        )


def _prepare_runtime(destination: Path) -> None:
    adapter = destination / "scripts" / "official_linear_mcp_bridge.py"
    _run_runtime_step(adapter, offline=False)
    _run_runtime_step(adapter, offline=True)


def _install(args: argparse.Namespace) -> dict[str, Any]:
    source = _source_skill_root()
    destination = _skills_root(args.skills_root) / SKILL_NAME
    config_path = _config_path(args.config)
    connections = _parse_connections(args.connection)
    aliases = tuple(alias for alias, _reference in connections)
    if not _valid_account(args.op_account):
        raise InstallError(
            "invalid_account", "The explicit 1Password account selector is invalid."
        )
    installed = _load_marker(destination)
    marker_is_private = (
        installed is None
        or stat.S_IMODE((destination / MARKER_NAME).stat().st_mode) == MARKER_MODE
    )
    if args.resume_paused and installed is None:
        raise InstallError(
            "resume_unavailable",
            "Paused managed MCP connections are not installed at this destination.",
        )
    current, mode, parsed = _read_config(config_path)
    desired_block = _managed_block(destination, connections, args.op_account)
    source_hash = _tree_hash(source)

    if installed is None:
        _assert_no_alias_conflict(parsed, aliases)
        desired_segment = _managed_segment(current, desired_block)
        desired_config = current + desired_segment
    else:
        old_segment = installed["config_segment"].encode("utf-8")
        if args.resume_paused:
            if (
                installed["normalized_connections"] != connections
                or installed["op_account"] != args.op_account
            ):
                raise InstallError(
                    "resume_identity_mismatch",
                    "Resume inputs must exactly match the installed managed connections.",
                )
            paused_segment = _paused_managed_segment(
                old_segment, len(installed["normalized_connections"])
            )
            prefix, suffix = _split_exact_segment(current, paused_segment)
        else:
            prefix, suffix = _split_exact_segment(current, old_segment)
        base_config = prefix + suffix
        try:
            parsed_base = (
                tomllib.loads(base_config.decode("utf-8"))
                if base_config.strip()
                else {}
            )
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise InstallError(
                "config_invalid",
                "The unmanaged Codex config bytes are not valid UTF-8 TOML.",
            ) from exc
        _assert_no_alias_conflict(parsed_base, aliases)
        desired_segment = _managed_segment(prefix, desired_block)
        if args.resume_paused and desired_segment != old_segment:
            raise InstallError(
                "managed_config_drift",
                "The managed MCP connection config cannot be resumed safely.",
            )
        desired_config = prefix + desired_segment + suffix

    desired_marker = _marker_payload(
        source_hash, desired_segment, connections, args.op_account
    )
    skill_change = (
        args.resume_paused
        or installed is None
        or installed["source_hash"] != source_hash
        or installed["normalized_connections"] != connections
        or installed["op_account"] != args.op_account
        or installed["config_segment"] != desired_segment.decode("utf-8")
        or not marker_is_private
    )
    config_change = current != desired_config
    state_actions = []
    if skill_change:
        state_actions.append("install-or-update-skill")
    config_action = (
        "resume-exact-paused-connection-block"
        if args.resume_paused
        else "add-or-update-exact-connection-block"
    )
    actions = [*state_actions, "prepare-credential-free-runtime"]
    actions.append("verify-configured-offline-runtime")
    if config_change:
        state_actions.append(config_action)
        actions.append(config_action)
    if not args.apply:
        return {
            "ok": True,
            "mode": "dry-run",
            "status": "planned",
            "aliases": list(aliases),
            "actions": actions,
        }

    staging_root: Path | None = None
    backup: Path | None = None
    if skill_change:
        try:
            staging_root, staged = _stage_skill(source, destination, desired_marker)
            backup = _install_staged_skill(staged, destination)
        except OSError as exc:
            raise InstallError(
                "skill_apply_failed", "The skill could not be installed atomically."
            ) from exc
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
    try:
        _prepare_runtime(destination)
        if config_change:
            _atomic_write(config_path, desired_config, mode)
    except InstallError:
        if skill_change:
            _restore_skill(destination, backup)
        raise
    except OSError as exc:
        if skill_change:
            _restore_skill(destination, backup)
        if args.resume_paused:
            try:
                if config_path.read_bytes() != current:
                    _atomic_write(config_path, current, mode)
            except OSError as restore_exc:
                raise InstallError(
                    "resume_restore_failed",
                    "Resume failed and the paused config could not be restored.",
                ) from restore_exc
        raise InstallError(
            "config_apply_failed",
            "The exact connection config block could not be applied atomically.",
        ) from exc
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return {
        "ok": True,
        "mode": "apply",
        "status": (
            "installed"
            if installed is None
            else "resumed"
            if args.resume_paused
            else "updated"
            if state_actions
            else "verified"
        ),
        "aliases": list(aliases),
    }


def _rollback(args: argparse.Namespace) -> dict[str, Any]:
    destination = _skills_root(args.skills_root) / SKILL_NAME
    config_path = _config_path(args.config)
    installed = _load_marker(destination)
    current, mode, _parsed = _read_config(config_path)
    if installed is None:
        return {
            "ok": True,
            "mode": "apply" if args.apply else "dry-run",
            "status": "no-op",
            "aliases": [],
        }
    aliases = tuple(alias for alias, _reference in installed["normalized_connections"])
    segment = installed["config_segment"].encode("utf-8")
    prefix, suffix = _split_exact_segment(current, segment)
    desired_config = prefix + suffix
    if not args.apply:
        return {
            "ok": True,
            "mode": "dry-run",
            "status": "planned",
            "aliases": list(aliases),
            "actions": ["remove-exact-connection-block", "remove-managed-skill"],
        }

    try:
        _atomic_write(config_path, desired_config, mode)
    except OSError as exc:
        raise InstallError(
            "config_apply_failed",
            "The exact connection config block could not be removed atomically.",
        ) from exc
    removal = destination.parent / f".official-linear-remove-{uuid.uuid4().hex}"
    try:
        os.replace(destination, removal)
    except OSError as exc:
        try:
            _atomic_write(config_path, current, mode)
        except OSError as restore_exc:
            raise InstallError(
                "rollback_restore_failed",
                "Rollback failed and the prior config could not be restored.",
            ) from restore_exc
        raise InstallError(
            "skill_remove_failed", "The managed skill could not be removed safely."
        ) from exc
    shutil.rmtree(removal)
    return {
        "ok": True,
        "mode": "apply",
        "status": "removed",
        "aliases": list(aliases),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument(
        "--connection",
        action="append",
        required=True,
        help="Explicit lowercase ALIAS=OP_REFERENCE; repeat for each process.",
    )
    install.add_argument("--op-account", required=True)
    install.add_argument(
        "--resume-paused",
        action="store_true",
        help=(
            "Resume only an exact installer-managed block whose enabled lines were "
            "all changed to false."
        ),
    )
    rollback = subparsers.add_parser("rollback")
    for subparser in (install, rollback):
        subparser.add_argument("--skills-root")
        subparser.add_argument("--config")
        subparser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = _install(args) if args.command == "install" else _rollback(args)
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
                        "code": "installer_failed",
                        "message": "The installer failed closed without exposing details.",
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
