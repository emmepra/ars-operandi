#!/usr/bin/env python3
"""Plan and apply exact native Codex OAuth configuration for Linear MCP."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Mapping


SKILL_NAME = "official-linear-mcp-bridge"
MARKER_NAME = ".ars-operandi-install.json"
MARKER_ID = "ars-operandi.official-linear-mcp-bridge"
MARKER_VERSION = 3
LEGACY_MARKER_VERSION = 2
MARKER_MODE = 0o600
STATE_MODE = 0o700
STAGED_STATE_NAME = ".ars-operandi-official-linear-native-v3"
RECOVERY_PREIMAGE_NAME = "recovery-preimage.toml"
RECOVERY_PREIMAGE_MODE = 0o600
MCP_REMOTE_COMMAND_SUFFIX = (
    "/mcp-remote/ars-operandi-1.0.0/node_modules/.bin/mcp-remote"
)
LINEAR_MCP_ENDPOINT = "https://mcp.linear.app/mcp"
BLOCK_BEGIN = "# BEGIN ars-operandi official-linear native connections v3"
BLOCK_END = "# END ars-operandi official-linear native connections v3"
LEGACY_BLOCK_BEGIN = "# BEGIN ars-operandi official-linear-mcp-bridge connections v2"
LEGACY_BLOCK_END = "# END ars-operandi official-linear-mcp-bridge connections v2"
KEYRING_BEGIN = "# BEGIN ars-operandi MCP OAuth keyring invariant v3"
KEYRING_END = "# END ars-operandi MCP OAuth keyring invariant v3"
KEYRING_SEGMENT = (
    f'{KEYRING_BEGIN}\nmcp_oauth_credentials_store = "keyring"\n{KEYRING_END}\n'
).encode("utf-8")
_ALIAS_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_ACCOUNT_PATTERN = re.compile(
    r"(?=.{3,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_REFERENCE_SEGMENT_PATTERN = re.compile(
    r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2}){1,128}\Z"
)
_MISSING = object()


class InstallError(RuntimeError):
    """A fail-closed installer error whose text is safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _parse_aliases(values: list[str]) -> tuple[str, str]:
    if (
        len(values) != 2
        or len(set(values)) != 2
        or any(not _ALIAS_PATTERN.fullmatch(value) for value in values)
    ):
        raise InstallError(
            "invalid_aliases",
            "Exactly two distinct lowercase MCP aliases are required.",
        )
    return values[0], values[1]


def _valid_account(value: str) -> bool:
    return bool(_ACCOUNT_PATTERN.fullmatch(value))


def _valid_reference(value: str) -> bool:
    if len(value) > 512 or not value.startswith("op://"):
        return False
    segments = value[5:].split("/")
    return len(segments) in {3, 4} and all(
        _REFERENCE_SEGMENT_PATTERN.fullmatch(segment) for segment in segments
    )


def _parse_legacy_connections(raw: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list) or not raw:
        raise InstallError(
            "legacy_state_invalid", "The legacy installer state is not exact."
        )
    connections: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise InstallError(
                "legacy_state_invalid", "The legacy installer state is not exact."
            )
        alias = item.get("alias")
        reference = item.get("reference")
        if (
            not isinstance(alias, str)
            or not _ALIAS_PATTERN.fullmatch(alias)
            or not isinstance(reference, str)
            or not _valid_reference(reference)
        ):
            raise InstallError(
                "legacy_state_invalid", "The legacy installer state is not exact."
            )
        connections.append((alias, reference))
    aliases = [alias for alias, _reference in connections]
    references = [reference for _alias, reference in connections]
    if len(set(aliases)) != len(aliases) or len(set(references)) != len(references):
        raise InstallError(
            "legacy_state_invalid", "The legacy installer state is not exact."
        )
    return tuple(connections)


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


def _managed_block(aliases: tuple[str, str]) -> bytes:
    lines = [BLOCK_BEGIN]
    for alias in aliases:
        lines.extend(
            [
                f"[mcp_servers.{alias}]",
                f"url = {_toml_string(LINEAR_MCP_ENDPOINT)}",
                'auth = "oauth"',
                "enabled = true",
                'default_tools_approval_mode = "writes"',
                "startup_timeout_sec = 60.0",
                "",
            ]
        )
    lines.append(BLOCK_END)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _read_config(path: Path) -> tuple[bytes, int, Mapping[str, Any], bool]:
    if not path.exists():
        return b"", 0o600, {}, False
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
    return data, stat.S_IMODE(path.stat().st_mode), parsed, True


def _parse_config_bytes(data: bytes) -> Mapping[str, Any]:
    try:
        decoded = data.decode("utf-8")
        return tomllib.loads(decoded) if decoded.strip() else {}
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallError(
            "config_invalid", "The unmanaged Codex config bytes are not valid TOML."
        ) from exc


def _assert_recovery_preimage_unchanged(path: Path, data: bytes, mode: int) -> None:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.read_bytes() != data
        or stat.S_IMODE(path.stat().st_mode) != mode
    ):
        raise InstallError(
            "recovery_preimage_changed",
            "The recovery preimage changed after planning; refusing mutation.",
        )


def _assert_locked_config_exact(
    path: Path, descriptor: int, data: bytes, mode: int
) -> None:
    descriptor_stat = os.fstat(descriptor)
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise InstallError(
            "recovery_preimage_changed",
            "The recovery config changed at commit time; refusing mutation.",
        ) from exc
    os.lseek(descriptor, 0, os.SEEK_SET)
    descriptor_data = b""
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        descriptor_data += chunk
    if (
        not stat.S_ISREG(descriptor_stat.st_mode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino)
        != (path_stat.st_dev, path_stat.st_ino)
        or descriptor_data != data
        or path.read_bytes() != data
        or stat.S_IMODE(path_stat.st_mode) != mode
    ):
        raise InstallError(
            "recovery_preimage_changed",
            "The recovery config changed at commit time; refusing mutation.",
        )


@contextmanager
def _locked_config(path: Path, data: bytes, mode: int):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallError(
            "recovery_preimage_changed",
            "The recovery config is unavailable at commit time.",
        ) from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _assert_locked_config_exact(path, descriptor, data, mode)
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_compare_and_write(
    path: Path,
    expected: bytes,
    expected_mode: int,
    desired: bytes,
    desired_mode: int,
) -> None:
    with _locked_config(path, expected, expected_mode) as descriptor:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(temporary_descriptor, "wb") as handle:
                handle.write(desired)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, desired_mode)
            _replace_locked_config(temporary, path, descriptor, expected, expected_mode)
        finally:
            temporary.unlink(missing_ok=True)


def _replace_locked_config(
    temporary: Path,
    path: Path,
    descriptor: int,
    expected: bytes,
    expected_mode: int,
) -> None:
    _assert_locked_config_exact(path, descriptor, expected, expected_mode)
    os.replace(temporary, path)


def _mcp_servers(parsed: Mapping[str, Any]) -> Mapping[str, Any]:
    value = parsed.get("mcp_servers", {})
    if not isinstance(value, Mapping):
        raise InstallError(
            "config_invalid", "The Codex MCP server table is not a mapping."
        )
    return value


def _assert_no_alias_conflict(
    parsed: Mapping[str, Any], aliases: tuple[str, str]
) -> None:
    servers = _mcp_servers(parsed)
    if any(alias in servers for alias in aliases):
        raise InstallError(
            "connection_alias_conflict",
            "A requested MCP alias already exists outside managed state.",
        )


def _keyring_plan(data: bytes, parsed: Mapping[str, Any]) -> tuple[str, bytes]:
    value = parsed.get("mcp_oauth_credentials_store", _MISSING)
    has_marker = KEYRING_BEGIN.encode() in data or KEYRING_END.encode() in data
    if value is _MISSING:
        if has_marker:
            raise InstallError(
                "managed_config_drift",
                "The managed OAuth keyring invariant has drifted.",
            )
        return "managed", KEYRING_SEGMENT + data
    if value != "keyring":
        raise InstallError(
            "oauth_store_conflict",
            "MCP OAuth credentials must already use the operating-system keyring.",
        )
    if has_marker:
        raise InstallError(
            "unmanaged_keyring_conflict",
            "A managed-looking OAuth keyring segment exists without matching state.",
        )
    return "preexisting", data


def _managed_segment(prefix: bytes, block: bytes) -> bytes:
    separator = b"" if not prefix or prefix.endswith(b"\n") else b"\n"
    return separator + block


def _replacement_segment(legacy_segment: bytes, block: bytes) -> bytes:
    begin = LEGACY_BLOCK_BEGIN.encode("utf-8")
    if legacy_segment.count(begin) != 1:
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config marker is invalid."
        )
    prefix = legacy_segment[: legacy_segment.index(begin)]
    if prefix not in {b"", b"\n"}:
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config marker is invalid."
        )
    return prefix + block


def _split_exact_segment(data: bytes, segment: bytes) -> tuple[bytes, bytes]:
    if not segment or data.count(segment) != 1:
        raise InstallError(
            "managed_config_drift",
            "The managed native MCP config has drifted; refusing mutation.",
        )
    index = data.index(segment)
    return data[:index], data[index + len(segment) :]


def _fingerprints(aliases: tuple[str, str]) -> list[str]:
    return [hashlib.sha256(alias.encode("utf-8")).hexdigest()[:12] for alias in aliases]


def _result(
    *,
    mode: str,
    status: str,
    aliases: tuple[str, str],
    actions: list[str] | None = None,
    preimage_sha256: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "status": status,
        "alias_count": 2,
        "alias_fingerprints": _fingerprints(aliases),
    }
    if actions is not None:
        payload["actions"] = actions
    if preimage_sha256 is not None:
        payload["preimage_sha256"] = preimage_sha256
    return payload


def _marker_payload(
    *,
    source_hash: str,
    aliases: tuple[str, str],
    native_segment: bytes,
    keyring_ownership: str,
    config_preexisted: bool,
    lifecycle: str,
    legacy_marker_sha256: str | None = None,
    legacy_segment_sha256: str | None = None,
    recovery_preimage_sha256: str | None = None,
    recovery_preimage_mode: int | None = None,
    recovery_native_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "installer": MARKER_ID,
        "version": MARKER_VERSION,
        "source_hash": source_hash,
        "aliases": list(aliases),
        "native_segment": native_segment.decode("utf-8"),
        "native_segment_sha256": hashlib.sha256(native_segment).hexdigest(),
        "keyring_ownership": keyring_ownership,
        "config_preexisted": config_preexisted,
        "keyring_segment_sha256": (
            hashlib.sha256(KEYRING_SEGMENT).hexdigest()
            if keyring_ownership == "managed"
            else None
        ),
        "lifecycle": lifecycle,
        "legacy_marker_sha256": legacy_marker_sha256,
        "legacy_segment_sha256": legacy_segment_sha256,
        "recovery_preimage_sha256": recovery_preimage_sha256,
        "recovery_preimage_mode": recovery_preimage_mode,
        "recovery_native_sha256": recovery_native_sha256,
    }


def _read_marker_file(path: Path) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise InstallError(
            "unmanaged_skill_conflict", "The installer marker is unavailable."
        )
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(
            "unmanaged_skill_conflict", "The installer marker is invalid."
        ) from exc
    if not isinstance(payload, dict):
        raise InstallError(
            "unmanaged_skill_conflict", "The installer marker is invalid."
        )
    return raw, payload


def _marker_version(destination: Path) -> int | None:
    if not destination.exists():
        return None
    if destination.is_symlink() or not destination.is_dir():
        raise InstallError(
            "unmanaged_skill_conflict",
            "The skill destination exists but is not installer-managed.",
        )
    _raw, payload = _read_marker_file(destination / MARKER_NAME)
    version = payload.get("version")
    return version if isinstance(version, int) else -1


def _load_legacy(destination: Path) -> tuple[bytes, dict[str, Any]]:
    if _marker_version(destination) != LEGACY_MARKER_VERSION:
        raise InstallError(
            "legacy_state_unavailable",
            "An exact installer-managed v2 bridge is required for migration.",
        )
    marker_path = destination / MARKER_NAME
    raw, payload = _read_marker_file(marker_path)
    segment = payload.get("config_segment")
    _parse_legacy_connections(payload.get("connections"))
    account = payload.get("op_account")
    if (
        payload.get("installer") != MARKER_ID
        or payload.get("version") != LEGACY_MARKER_VERSION
        or not isinstance(payload.get("source_hash"), str)
        or not isinstance(segment, str)
        or not isinstance(account, str)
        or not _valid_account(account)
        or payload.get("config_segment_sha256")
        != hashlib.sha256(segment.encode("utf-8")).hexdigest()
        or segment.count(LEGACY_BLOCK_BEGIN) != 1
        or segment.count(LEGACY_BLOCK_END) != 1
        or stat.S_IMODE(marker_path.stat().st_mode) != MARKER_MODE
    ):
        raise InstallError(
            "legacy_state_invalid", "The legacy installer state is not exact."
        )
    if _tree_hash(destination) != payload["source_hash"]:
        raise InstallError(
            "managed_skill_drift",
            "The legacy rollback skill has local drift; refusing mutation.",
        )
    return raw, payload


def _validate_v3_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_aliases = payload.get("aliases")
    try:
        aliases = _parse_aliases(list(raw_aliases))
    except (InstallError, TypeError):
        aliases = ()
    segment = payload.get("native_segment")
    ownership = payload.get("keyring_ownership")
    lifecycle = payload.get("lifecycle")
    if (
        payload.get("installer") != MARKER_ID
        or payload.get("version") != MARKER_VERSION
        or not isinstance(payload.get("source_hash"), str)
        or len(aliases) != 2
        or not isinstance(segment, str)
        or payload.get("native_segment_sha256")
        != hashlib.sha256(segment.encode("utf-8")).hexdigest()
        or segment.encode("utf-8") != _replacement_or_append_block(segment, aliases)
        or ownership not in {"managed", "preexisting"}
        or not isinstance(payload.get("config_preexisted"), bool)
        or lifecycle not in {"installed", "staged-v2", "finalized-v2"}
    ):
        raise InstallError(
            "managed_state_drift", "The native installer state is invalid."
        )
    expected_keyring_hash = (
        hashlib.sha256(KEYRING_SEGMENT).hexdigest() if ownership == "managed" else None
    )
    if payload.get("keyring_segment_sha256") != expected_keyring_hash:
        raise InstallError(
            "managed_state_drift", "The native keyring state is invalid."
        )
    recovery_values = (
        payload.get("recovery_preimage_sha256"),
        payload.get("recovery_preimage_mode"),
        payload.get("recovery_native_sha256"),
    )
    if any(value is not None for value in recovery_values):
        preimage_hash, preimage_mode, native_hash = recovery_values
        if (
            lifecycle != "staged-v2"
            or not isinstance(preimage_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", preimage_hash)
            or not isinstance(native_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", native_hash)
            or not isinstance(preimage_mode, int)
            or preimage_mode < 0
            or preimage_mode > 0o777
        ):
            raise InstallError(
                "managed_state_drift", "The recovery installer state is invalid."
            )
    normalized = dict(payload)
    normalized["normalized_aliases"] = aliases
    return normalized


def _replacement_or_append_block(segment: str, aliases: tuple[str, str]) -> bytes:
    raw = segment.encode("utf-8")
    block = _managed_block(aliases)
    if raw == block or raw == b"\n" + block:
        return raw
    return b""


def _load_v3_destination(destination: Path) -> dict[str, Any] | None:
    version = _marker_version(destination)
    if version is None:
        return None
    if version != MARKER_VERSION:
        if version == LEGACY_MARKER_VERSION:
            raise InstallError(
                "migration_required",
                "Use the staged migration command for the installed v2 bridge.",
            )
        raise InstallError(
            "unmanaged_skill_conflict", "The skill installer marker is invalid."
        )
    marker_path = destination / MARKER_NAME
    _raw, payload = _read_marker_file(marker_path)
    normalized = _validate_v3_payload(payload)
    if stat.S_IMODE(marker_path.stat().st_mode) != MARKER_MODE:
        raise InstallError(
            "managed_state_drift", "The native installer marker mode has drifted."
        )
    if _tree_hash(destination) != normalized["source_hash"]:
        raise InstallError(
            "managed_skill_drift",
            "The managed config skill has local drift; refusing mutation.",
        )
    return normalized


def _state_path(skills_root: Path) -> Path:
    return skills_root / STAGED_STATE_NAME


def _load_staged_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_dir():
        raise InstallError(
            "managed_state_drift", "The staged migration state is unavailable."
        )
    if stat.S_IMODE(path.stat().st_mode) != STATE_MODE:
        raise InstallError(
            "managed_state_drift", "The staged migration state has drifted."
        )
    marker_path = path / MARKER_NAME
    _raw, payload = _read_marker_file(marker_path)
    if stat.S_IMODE(marker_path.stat().st_mode) != MARKER_MODE:
        raise InstallError(
            "managed_state_drift", "The staged migration marker has drifted."
        )
    normalized = _validate_v3_payload(payload)
    if normalized["lifecycle"] != "staged-v2":
        raise InstallError(
            "managed_state_drift", "The staged migration marker is invalid."
        )
    recovery_hash = normalized.get("recovery_preimage_sha256")
    expected_names = {MARKER_NAME}
    if recovery_hash is not None:
        expected_names.add(RECOVERY_PREIMAGE_NAME)
    if {item.name for item in path.iterdir()} != expected_names:
        raise InstallError(
            "managed_state_drift", "The staged migration state has drifted."
        )
    if recovery_hash is not None:
        preimage_path = path / RECOVERY_PREIMAGE_NAME
        if (
            preimage_path.is_symlink()
            or not preimage_path.is_file()
            or stat.S_IMODE(preimage_path.stat().st_mode) != RECOVERY_PREIMAGE_MODE
        ):
            raise InstallError(
                "managed_state_drift", "The recovery rollback copy has drifted."
            )
        preimage = preimage_path.read_bytes()
        if hashlib.sha256(preimage).hexdigest() != recovery_hash:
            raise InstallError(
                "managed_state_drift", "The recovery rollback copy has drifted."
            )
        normalized["recovery_preimage"] = preimage
    return normalized


def _assert_legacy_authority(
    destination: Path, state: Mapping[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    raw, legacy = _load_legacy(destination)
    segment = legacy["config_segment"].encode("utf-8")
    if (
        state.get("legacy_marker_sha256") != hashlib.sha256(raw).hexdigest()
        or state.get("legacy_segment_sha256") != hashlib.sha256(segment).hexdigest()
    ):
        raise InstallError(
            "legacy_state_drift", "The staged rollback authority has drifted."
        )
    return raw, legacy


def _assert_native_config_exact(
    current: bytes, parsed: Mapping[str, Any], marker: Mapping[str, Any]
) -> None:
    aliases = marker["normalized_aliases"]
    segment = marker["native_segment"].encode("utf-8")
    if segment != _replacement_or_append_block(marker["native_segment"], aliases):
        raise InstallError(
            "managed_state_drift", "The native installer state is invalid."
        )
    _split_exact_segment(current, segment)
    if parsed.get("mcp_oauth_credentials_store") != "keyring":
        raise InstallError(
            "managed_config_drift", "The OAuth keyring invariant has drifted."
        )
    if marker["keyring_ownership"] == "managed":
        _split_exact_segment(current, KEYRING_SEGMENT)
    servers = _mcp_servers(parsed)
    expected = {
        "url": LINEAR_MCP_ENDPOINT,
        "auth": "oauth",
        "enabled": True,
        "default_tools_approval_mode": "writes",
        "startup_timeout_sec": 60.0,
    }
    if any(servers.get(alias) != expected for alias in aliases):
        raise InstallError(
            "managed_config_drift", "The managed native MCP aliases have drifted."
        )


def _table_spans(data: bytes) -> dict[str, tuple[int, int]]:
    any_header = re.compile(
        rb"(?m)^[ \t]*(?:\[\[[^\]\r\n]+\]\]|\[[^\]\r\n]+\])[ \t]*(?:#[^\r\n]*)?\r?$"
    )
    mcp_header = re.compile(
        rb"^[ \t]*\[mcp_servers\.([a-z0-9-]+)\][ \t]*(?:#[^\r\n]*)?\r?$"
    )
    nested_header = re.compile(
        rb"^[ \t]*\[mcp_servers\.([a-z0-9-]+)\.[^\]\r\n]+\][ \t]*(?:#[^\r\n]*)?\r?$"
    )
    matches = list(any_header.finditer(data))
    spans: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        owned = mcp_header.fullmatch(match.group(0))
        if owned is None:
            continue
        alias = owned.group(1).decode("ascii")
        if alias in spans:
            raise InstallError(
                "recovery_preimage_invalid", "The recovery alias tables are ambiguous."
            )
        next_index = index + 1
        while next_index < len(matches):
            nested = nested_header.fullmatch(matches[next_index].group(0))
            if nested is None or nested.group(1) != owned.group(1):
                break
            next_index += 1
        end = matches[next_index].start() if next_index < len(matches) else len(data)
        spans[alias] = (match.start(), end)
    return spans


def _assert_safe_rollback_segment(segment: bytes) -> None:
    lowered = segment.lower()
    forbidden = (
        b"authorization",
        b"bearer",
        b"access_token",
        b"refresh_token",
        b"code_verifier",
        b"client_secret",
        b"http_headers",
        b"bearer_token_env_var",
        b"--debug",
        b"/authorize?",
        b"$(",
        b"${",
        b"`",
    )
    if any(value in lowered for value in forbidden):
        raise InstallError(
            "recovery_preimage_unsafe", "The recovery rollback input is unsafe."
        )


def _assert_nested_env_table(alias: str, segment: bytes) -> None:
    header = f"[mcp_servers.{alias}.env]".encode("utf-8")
    if segment.count(header) != 1 or re.search(rb"(?m)^[ \t]*env[ \t]*=", segment):
        raise InstallError(
            "recovery_preimage_invalid",
            "The recovery runtime environment table is not exact.",
        )


def _validate_mcp_remote_alias(value: object) -> tuple[int, str, str]:
    if not isinstance(value, Mapping):
        raise InstallError(
            "recovery_preimage_invalid", "The recovery runtime config is invalid."
        )
    required = {
        "command",
        "args",
        "env",
        "default_tools_approval_mode",
        "startup_timeout_sec",
    }
    args = value.get("args")
    env = value.get("env")
    config_dir = env.get("MCP_REMOTE_CONFIG_DIR") if isinstance(env, Mapping) else None
    if (
        set(value) != required
        or not isinstance(value.get("command"), str)
        or not os.path.isabs(value["command"])
        or os.path.normpath(value["command"]) != value["command"]
        or not value["command"].endswith(MCP_REMOTE_COMMAND_SUFFIX)
        or any(
            character in value["command"]
            for character in ("\x00", "\n", "\r", "`", "$", "~")
        )
        or not isinstance(args, list)
        or len(args) != 6
        or args[0] != LINEAR_MCP_ENDPOINT
        or not isinstance(args[1], str)
        or not args[1].isdigit()
        or not 1024 <= int(args[1]) <= 65535
        or args[2:] != ["--transport", "http-only", "--host", "127.0.0.1"]
        or not isinstance(env, Mapping)
        or set(env) != {"ARS_MCP_AUTH_MODE", "MCP_REMOTE_CONFIG_DIR"}
        or env.get("ARS_MCP_AUTH_MODE") != "noninteractive"
        or not isinstance(config_dir, str)
        or not os.path.isabs(config_dir)
        or os.path.normpath(config_dir) != config_dir
        or any(
            character in config_dir for character in ("\x00", "\n", "\r", "`", "$", "~")
        )
        or value.get("default_tools_approval_mode") != "writes"
        or value.get("startup_timeout_sec") != 60.0
    ):
        raise InstallError(
            "recovery_preimage_invalid", "The recovery runtime config is invalid."
        )
    return int(args[1]), config_dir, value["command"]


def _paused_legacy_body(legacy: Mapping[str, Any]) -> bytes:
    segment = legacy["config_segment"].encode("utf-8")
    begin = (LEGACY_BLOCK_BEGIN + "\n").encode("utf-8")
    end = (LEGACY_BLOCK_END + "\n").encode("utf-8")
    if segment.count(begin) != 1 or segment.count(end) != 1:
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config marker is invalid."
        )
    begin_index = segment.index(begin)
    end_index = segment.index(end)
    if (
        begin_index > 1
        or end_index <= begin_index
        or end_index + len(end) != len(segment)
    ):
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config marker is invalid."
        )
    paused = segment[begin_index + len(begin) : end_index]
    enabled = b"enabled = true\n"
    expected = len(_parse_legacy_connections(legacy["connections"]))
    if paused.count(enabled) != expected:
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config segment is invalid."
        )
    return paused.replace(enabled, b"enabled = false\n")


def _historical_paused_legacy_body(legacy: Mapping[str, Any]) -> bytes:
    paused = _paused_legacy_body(legacy)
    canonical_policy = (
        b'default_tools_approval_mode = "writes"\nstartup_timeout_sec = 60.0\n'
    )
    historical_policy = (
        b'startup_timeout_sec = 60.0\ndefault_tools_approval_mode = "writes"\n'
    )
    expected = len(_parse_legacy_connections(legacy["connections"]))
    if paused.count(canonical_policy) != expected:
        raise InstallError(
            "legacy_state_invalid", "The legacy managed config segment is invalid."
        )
    return paused.replace(canonical_policy, historical_policy)


def _remove_ranges(data: bytes, ranges: list[tuple[int, int]]) -> bytes:
    ordered = sorted(ranges)
    if any(start < 0 or end <= start for start, end in ordered) or any(
        ordered[index][1] > ordered[index + 1][0] for index in range(len(ordered) - 1)
    ):
        raise InstallError(
            "recovery_preimage_invalid", "The recovery ownership ranges overlap."
        )
    pieces: list[bytes] = []
    cursor = 0
    for start, end in ordered:
        pieces.append(data[cursor:start])
        cursor = end
    pieces.append(data[cursor:])
    return b"".join(pieces)


def _recovery_plan(
    current: bytes,
    parsed: Mapping[str, Any],
    legacy: Mapping[str, Any],
    aliases: tuple[str, str],
) -> tuple[bytes, bytes, str]:
    if parsed.get("mcp_oauth_credentials_store", _MISSING) is not _MISSING:
        raise InstallError(
            "recovery_preimage_invalid",
            "The exact recovery preimage must not contain OAuth keyring state.",
        )
    begin_line = (LEGACY_BLOCK_BEGIN + "\n").encode("utf-8")
    end_line = (LEGACY_BLOCK_END + "\n").encode("utf-8")
    if current.count(begin_line) != 0 or current.count(end_line) != 1:
        raise InstallError(
            "recovery_preimage_invalid",
            "The orphaned legacy marker shape is not exact.",
        )
    paused_candidates = (
        _paused_legacy_body(legacy),
        _historical_paused_legacy_body(legacy),
    )
    matching_paused = [
        candidate
        for candidate in dict.fromkeys(paused_candidates)
        if current.count(candidate) == 1
    ]
    if len(matching_paused) != 1:
        raise InstallError(
            "recovery_preimage_invalid", "The paused legacy residue is not exact."
        )
    paused_legacy = matching_paused[0]
    paused_start = current.index(paused_legacy)
    paused_range = (paused_start, paused_start + len(paused_legacy))
    end_start = current.index(end_line)
    end_range = (end_start, end_start + len(end_line))
    if end_start <= paused_range[1]:
        raise InstallError(
            "recovery_preimage_invalid",
            "The orphaned legacy end marker is not separated from paused canaries.",
        )
    servers = _mcp_servers(parsed)
    ports_and_dirs = [
        _validate_mcp_remote_alias(servers.get(alias)) for alias in aliases
    ]
    if (
        len({port for port, _directory, _command in ports_and_dirs}) != 2
        or len({directory for _port, directory, _command in ports_and_dirs}) != 2
        or len({command for _port, _directory, command in ports_and_dirs}) != 1
    ):
        raise InstallError(
            "recovery_preimage_invalid", "The recovery profiles are not isolated."
        )
    spans = _table_spans(current)
    try:
        alias_ranges = [spans[alias] for alias in aliases]
    except KeyError as exc:
        raise InstallError(
            "recovery_preimage_invalid", "A recovery alias table is missing."
        ) from exc
    ordered_alias_ranges = sorted(alias_ranges)
    if ordered_alias_ranges[0][1] != ordered_alias_ranges[1][0]:
        raise InstallError(
            "recovery_preimage_invalid",
            "The recovery alias pair is not one contiguous owned segment.",
        )
    for alias, (start, finish) in zip(aliases, alias_ranges):
        _assert_safe_rollback_segment(current[start:finish])
        _assert_nested_env_table(alias, current[start:finish])
    base_config = _remove_ranges(current, [paused_range, end_range, *alias_ranges])
    base_parsed = _parse_config_bytes(base_config)
    _assert_no_alias_conflict(base_parsed, aliases)
    ownership, with_keyring = _keyring_plan(base_config, base_parsed)
    if ownership != "managed":
        raise InstallError(
            "recovery_preimage_invalid", "The recovery keyring ownership is not exact."
        )
    native_segment = _managed_segment(with_keyring, _managed_block(aliases))
    desired_config = with_keyring + native_segment
    _parse_config_bytes(desired_config)
    return desired_config, native_segment, ownership


def _stage_skill(
    source: Path, destination: Path, marker: Mapping[str, Any]
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
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker_path.chmod(MARKER_MODE)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return staging_root, staged


def _stage_state(
    destination: Path,
    marker: Mapping[str, Any],
    *,
    recovery_preimage: bytes | None = None,
) -> tuple[Path, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".official-linear-state-", dir=destination.parent)
    )
    staged = staging_root / STAGED_STATE_NAME
    try:
        staged.mkdir(mode=STATE_MODE)
        staged.chmod(STATE_MODE)
        marker_path = staged / MARKER_NAME
        marker_path.write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        marker_path.chmod(MARKER_MODE)
        if recovery_preimage is not None:
            preimage_path = staged / RECOVERY_PREIMAGE_NAME
            descriptor = os.open(
                preimage_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                RECOVERY_PREIMAGE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(recovery_preimage)
                handle.flush()
                os.fsync(handle.fileno())
            preimage_path.chmod(RECOVERY_PREIMAGE_MODE)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    return staging_root, staged


def _install_staged(staged: Path, destination: Path) -> Path | None:
    backup: Path | None = None
    if destination.exists():
        backup = destination.parent / f".official-linear-backup-{uuid.uuid4().hex}"
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    return backup


def _restore_directory(destination: Path, backup: Path | None) -> None:
    failed: Path | None = None
    if destination.exists():
        failed = destination.parent / f".official-linear-failed-{uuid.uuid4().hex}"
        os.replace(destination, failed)
    if backup is not None and backup.exists():
        os.replace(backup, destination)
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


def _restore_config_after_failure(
    path: Path,
    current: bytes,
    mode: int,
    preexisted: bool,
    expected_written: bytes,
) -> None:
    try:
        if preexisted:
            if path.exists() and path.read_bytes() == current:
                return
            _atomic_write(path, current, mode)
            return
        if not path.exists():
            return
        if path.read_bytes() != expected_written:
            raise OSError("config changed during failed atomic operation")
        path.unlink()
    except OSError as exc:
        raise InstallError(
            "atomic_restore_failed",
            "A filesystem failure occurred and the prior config could not be restored.",
        ) from exc


def _write_restored_config(
    path: Path, data: bytes, mode: int, *, should_exist: bool
) -> None:
    _atomic_write(path, data, mode)
    if not should_exist:
        path.unlink()


def _install(args: argparse.Namespace) -> dict[str, Any]:
    aliases = _parse_aliases(args.alias)
    source = _source_skill_root()
    skills_root = _skills_root(args.skills_root)
    destination = skills_root / SKILL_NAME
    if _load_staged_state(_state_path(skills_root)) is not None:
        raise InstallError(
            "migration_in_progress", "Finalize or restore the staged migration first."
        )
    installed = _load_v3_destination(destination)
    current, mode, parsed, current_preexisted = _read_config(_config_path(args.config))
    source_hash = _tree_hash(source)

    if installed is None:
        _assert_no_alias_conflict(parsed, aliases)
        ownership, with_keyring = _keyring_plan(current, parsed)
        native_segment = _managed_segment(with_keyring, _managed_block(aliases))
        desired_config = with_keyring + native_segment
        config_preexisted = current_preexisted
        lifecycle = "installed"
    else:
        if installed["normalized_aliases"] != aliases:
            raise InstallError(
                "alias_set_mismatch",
                "Requested aliases must exactly match the managed native aliases.",
            )
        _assert_native_config_exact(current, parsed, installed)
        ownership = installed["keyring_ownership"]
        native_segment = installed["native_segment"].encode("utf-8")
        desired_config = current
        config_preexisted = installed["config_preexisted"]
        lifecycle = installed["lifecycle"]

    marker = _marker_payload(
        source_hash=source_hash,
        aliases=aliases,
        native_segment=native_segment,
        keyring_ownership=ownership,
        config_preexisted=config_preexisted,
        lifecycle=lifecycle,
    )
    skill_change = installed is None or installed["source_hash"] != source_hash
    config_change = current != desired_config
    actions = []
    if skill_change:
        actions.append("install-credential-free-config-skill")
    if ownership == "managed" and installed is None:
        actions.append("add-keyring-invariant")
    if config_change:
        actions.append("add-exact-native-connection-block")
    if not actions:
        actions.append("verify-exact-native-state")
    if not args.apply:
        return _result(
            mode="dry-run", status="planned", aliases=aliases, actions=actions
        )

    staging_root: Path | None = None
    backup: Path | None = None
    if skill_change:
        try:
            staging_root, staged = _stage_skill(source, destination, marker)
            backup = _install_staged(staged, destination)
        except OSError as exc:
            raise InstallError(
                "skill_apply_failed",
                "The config skill could not be installed atomically.",
            ) from exc
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
    try:
        if config_change:
            _atomic_write(_config_path(args.config), desired_config, mode)
    except OSError as exc:
        _restore_config_after_failure(
            _config_path(args.config),
            current,
            mode,
            current_preexisted,
            desired_config,
        )
        if skill_change:
            _restore_directory(destination, backup)
        raise InstallError(
            "config_apply_failed",
            "The native MCP config could not be applied atomically.",
        ) from exc
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return _result(
        mode="apply",
        status="installed"
        if installed is None
        else "updated"
        if skill_change
        else "verified",
        aliases=aliases,
    )


def _migrate(args: argparse.Namespace) -> dict[str, Any]:
    aliases = _parse_aliases(args.alias)
    source = _source_skill_root()
    skills_root = _skills_root(args.skills_root)
    destination = skills_root / SKILL_NAME
    state_path = _state_path(skills_root)
    staged_state = _load_staged_state(state_path)
    current, mode, parsed, current_preexisted = _read_config(_config_path(args.config))
    legacy_raw, legacy = _load_legacy(destination)

    if staged_state is not None:
        if staged_state["normalized_aliases"] != aliases:
            raise InstallError(
                "alias_set_mismatch",
                "Requested aliases must exactly match the staged native aliases.",
            )
        _assert_legacy_authority(destination, staged_state)
        _assert_native_config_exact(current, parsed, staged_state)
        if staged_state["source_hash"] != _tree_hash(source):
            raise InstallError(
                "managed_state_drift",
                "The staged migration source has changed; restore before retrying.",
            )
        if not args.apply:
            return _result(
                mode="dry-run",
                status="planned",
                aliases=aliases,
                actions=["verify-staged-native-state"],
            )
        return _result(mode="apply", status="staged", aliases=aliases)

    legacy_segment = legacy["config_segment"].encode("utf-8")
    legacy_prefix, legacy_suffix = _split_exact_segment(current, legacy_segment)
    base_config = legacy_prefix + legacy_suffix
    base_parsed = _parse_config_bytes(base_config)
    _assert_no_alias_conflict(base_parsed, aliases)
    ownership, _with_keyring = _keyring_plan(base_config, base_parsed)
    native_segment = _replacement_segment(legacy_segment, _managed_block(aliases))
    native_without_keyring = legacy_prefix + native_segment + legacy_suffix
    desired_config = (
        KEYRING_SEGMENT + native_without_keyring
        if ownership == "managed"
        else native_without_keyring
    )
    _parse_config_bytes(desired_config)
    marker = _marker_payload(
        source_hash=_tree_hash(source),
        aliases=aliases,
        native_segment=native_segment,
        keyring_ownership=ownership,
        config_preexisted=current_preexisted,
        lifecycle="staged-v2",
        legacy_marker_sha256=hashlib.sha256(legacy_raw).hexdigest(),
        legacy_segment_sha256=hashlib.sha256(legacy_segment).hexdigest(),
    )
    actions = ["stage-exact-v2-rollback-authority"]
    if ownership == "managed":
        actions.append("add-keyring-invariant")
    actions.append("replace-v2-block-with-native-oauth-block")
    if not args.apply:
        return _result(
            mode="dry-run", status="planned", aliases=aliases, actions=actions
        )

    staging_root: Path | None = None
    try:
        staging_root, staged = _stage_state(state_path, marker)
        _install_staged(staged, state_path)
    except OSError as exc:
        raise InstallError(
            "state_apply_failed", "The staged migration state could not be installed."
        ) from exc
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
    try:
        _atomic_write(_config_path(args.config), desired_config, mode)
    except OSError as exc:
        _restore_config_after_failure(
            _config_path(args.config),
            current,
            mode,
            current_preexisted,
            desired_config,
        )
        shutil.rmtree(state_path, ignore_errors=True)
        raise InstallError(
            "config_apply_failed", "The native migration config could not be applied."
        ) from exc
    return _result(mode="apply", status="staged", aliases=aliases)


def _recover_migrate(args: argparse.Namespace) -> dict[str, Any]:
    aliases = _parse_aliases(args.alias)
    source = _source_skill_root()
    skills_root = _skills_root(args.skills_root)
    destination = skills_root / SKILL_NAME
    state_path = _state_path(skills_root)
    if _load_staged_state(state_path) is not None:
        raise InstallError(
            "migration_in_progress", "Finalize or restore the staged migration first."
        )
    current, mode, parsed, current_preexisted = _read_config(_config_path(args.config))
    legacy_raw, legacy = _load_legacy(destination)
    desired_config, native_segment, ownership = _recovery_plan(
        current, parsed, legacy, aliases
    )
    preimage_hash = hashlib.sha256(current).hexdigest()
    marker = _marker_payload(
        source_hash=_tree_hash(source),
        aliases=aliases,
        native_segment=native_segment,
        keyring_ownership=ownership,
        config_preexisted=current_preexisted,
        lifecycle="staged-v2",
        legacy_marker_sha256=hashlib.sha256(legacy_raw).hexdigest(),
        legacy_segment_sha256=hashlib.sha256(
            legacy["config_segment"].encode("utf-8")
        ).hexdigest(),
        recovery_preimage_sha256=preimage_hash,
        recovery_preimage_mode=mode,
        recovery_native_sha256=hashlib.sha256(desired_config).hexdigest(),
    )
    actions = [
        "stage-owner-only-byte-exact-recovery-copy",
        "retire-exact-paused-v2-residue",
        "retire-exact-mcp-remote-alias-pair",
        "add-keyring-invariant",
        "add-exact-native-connection-block",
    ]
    if not args.apply:
        return _result(
            mode="dry-run",
            status="planned",
            aliases=aliases,
            actions=actions,
            preimage_sha256=preimage_hash,
        )
    if args.expected_config_sha256 != preimage_hash:
        raise InstallError(
            "recovery_preimage_changed",
            "The reviewed recovery preimage digest does not match current config.",
        )
    staging_root: Path | None = None
    try:
        staging_root, staged = _stage_state(
            state_path, marker, recovery_preimage=current
        )
        _install_staged(staged, state_path)
    except OSError as exc:
        raise InstallError(
            "state_apply_failed", "The recovery migration state could not be installed."
        ) from exc
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
    try:
        _atomic_compare_and_write(
            _config_path(args.config),
            current,
            mode,
            desired_config,
            mode,
        )
    except (InstallError, OSError) as exc:
        shutil.rmtree(state_path, ignore_errors=True)
        if isinstance(exc, InstallError):
            raise
        raise InstallError(
            "config_apply_failed", "The recovery migration config could not be applied."
        ) from exc
    return _result(mode="apply", status="staged", aliases=aliases)


def _config_without_native(
    current: bytes, marker: Mapping[str, Any]
) -> tuple[bytes, bytes, bytes]:
    working = current
    if marker["keyring_ownership"] == "managed":
        key_prefix, key_suffix = _split_exact_segment(working, KEYRING_SEGMENT)
        working = key_prefix + key_suffix
    segment = marker["native_segment"].encode("utf-8")
    prefix, suffix = _split_exact_segment(working, segment)
    return working, prefix, suffix


def _restore(args: argparse.Namespace) -> dict[str, Any]:
    skills_root = _skills_root(args.skills_root)
    destination = skills_root / SKILL_NAME
    state_path = _state_path(skills_root)
    staged = _load_staged_state(state_path)
    current, mode, parsed, current_preexisted = _read_config(_config_path(args.config))

    if staged is not None:
        _assert_native_config_exact(current, parsed, staged)
        _legacy_raw, legacy = _assert_legacy_authority(destination, staged)
        recovery_preimage = staged.get("recovery_preimage")
        if recovery_preimage is not None:
            if hashlib.sha256(current).hexdigest() != staged["recovery_native_sha256"]:
                raise InstallError(
                    "managed_config_drift",
                    "The recovery native config has drifted; refusing restore.",
                )
            desired_config = recovery_preimage
            desired_mode = staged["recovery_preimage_mode"]
        else:
            _working, prefix, suffix = _config_without_native(current, staged)
            desired_config = prefix + legacy["config_segment"].encode("utf-8") + suffix
            desired_mode = mode
        _parse_config_bytes(desired_config)
        aliases = staged["normalized_aliases"]
        actions = [
            "restore-exact-v2-connection-block",
            "remove-staged-native-state",
        ]
        if staged["keyring_ownership"] == "managed":
            actions.insert(0, "remove-managed-keyring-invariant")
        if not args.apply:
            return _result(
                mode="dry-run", status="planned", aliases=aliases, actions=actions
            )
        removal = state_path.parent / f".official-linear-remove-{uuid.uuid4().hex}"
        if recovery_preimage is not None:
            try:
                os.replace(state_path, removal)
                _atomic_compare_and_write(
                    _config_path(args.config),
                    current,
                    mode,
                    desired_config,
                    desired_mode,
                )
            except (InstallError, OSError) as exc:
                if removal.exists() and not state_path.exists():
                    os.replace(removal, state_path)
                if isinstance(exc, InstallError):
                    raise
                raise InstallError(
                    "restore_failed",
                    "The recovery migration could not be restored atomically.",
                ) from exc
            shutil.rmtree(removal)
            return _result(mode="apply", status="legacy-restored", aliases=aliases)
        try:
            os.replace(state_path, removal)
            _write_restored_config(
                _config_path(args.config),
                desired_config,
                desired_mode,
                should_exist=staged["config_preexisted"] or bool(desired_config),
            )
        except OSError as exc:
            _restore_config_after_failure(
                _config_path(args.config),
                current,
                mode,
                current_preexisted,
                desired_config,
            )
            if removal.exists() and not state_path.exists():
                os.replace(removal, state_path)
            raise InstallError(
                "restore_failed",
                "The staged migration could not be restored atomically.",
            ) from exc
        shutil.rmtree(removal)
        return _result(mode="apply", status="legacy-restored", aliases=aliases)

    installed = _load_v3_destination(destination)
    if installed is None:
        return {
            "ok": True,
            "mode": "apply" if args.apply else "dry-run",
            "status": "no-op",
            "alias_count": 0,
            "alias_fingerprints": [],
        }
    _assert_native_config_exact(current, parsed, installed)
    _working, prefix, suffix = _config_without_native(current, installed)
    desired_config = prefix + suffix
    _parse_config_bytes(desired_config)
    aliases = installed["normalized_aliases"]
    actions = ["remove-exact-native-connection-block", "remove-managed-config-skill"]
    if installed["keyring_ownership"] == "managed":
        actions.insert(0, "remove-managed-keyring-invariant")
    if not args.apply:
        return _result(
            mode="dry-run", status="planned", aliases=aliases, actions=actions
        )
    removal = destination.parent / f".official-linear-remove-{uuid.uuid4().hex}"
    try:
        os.replace(destination, removal)
        _write_restored_config(
            _config_path(args.config),
            desired_config,
            mode,
            should_exist=installed["config_preexisted"] or bool(desired_config),
        )
    except OSError as exc:
        _restore_config_after_failure(
            _config_path(args.config),
            current,
            mode,
            current_preexisted,
            desired_config,
        )
        if removal.exists() and not destination.exists():
            os.replace(removal, destination)
        raise InstallError(
            "restore_failed", "The native install could not be restored atomically."
        ) from exc
    shutil.rmtree(removal)
    return _result(mode="apply", status="removed", aliases=aliases)


def _finalize(args: argparse.Namespace) -> dict[str, Any]:
    source = _source_skill_root()
    skills_root = _skills_root(args.skills_root)
    destination = skills_root / SKILL_NAME
    state_path = _state_path(skills_root)
    staged = _load_staged_state(state_path)
    if staged is None:
        raise InstallError(
            "finalize_unavailable", "No exact staged v2 migration is available."
        )
    _assert_legacy_authority(destination, staged)
    current, mode, parsed, _current_preexisted = _read_config(_config_path(args.config))
    _assert_native_config_exact(current, parsed, staged)
    if staged.get("recovery_preimage") is not None and (
        hashlib.sha256(current).hexdigest() != staged["recovery_native_sha256"]
    ):
        raise InstallError(
            "managed_config_drift",
            "The recovery native config has drifted; refusing finalization.",
        )
    source_hash = _tree_hash(source)
    if staged["source_hash"] != source_hash:
        raise InstallError(
            "managed_state_drift",
            "The staged migration source has changed; restore before retrying.",
        )
    aliases = staged["normalized_aliases"]
    marker = _marker_payload(
        source_hash=source_hash,
        aliases=aliases,
        native_segment=staged["native_segment"].encode("utf-8"),
        keyring_ownership=staged["keyring_ownership"],
        config_preexisted=staged["config_preexisted"],
        lifecycle="finalized-v2",
    )
    actions = [
        "replace-legacy-runtime-with-credential-free-config-skill",
        "remove-pre-smoke-rollback-authority",
    ]
    if not args.apply:
        return _result(
            mode="dry-run", status="planned", aliases=aliases, actions=actions
        )

    staging_root: Path | None = None
    legacy_backup: Path | None = None
    state_backup = state_path.parent / f".official-linear-state-{uuid.uuid4().hex}"
    recovery = staged.get("recovery_preimage") is not None
    lock = (
        _locked_config(_config_path(args.config), current, mode)
        if recovery
        else nullcontext(None)
    )
    with lock as config_descriptor:
        try:
            staging_root, staged_skill = _stage_skill(source, destination, marker)
            legacy_backup = _install_staged(staged_skill, destination)
            os.replace(state_path, state_backup)
            if config_descriptor is not None:
                _assert_locked_config_exact(
                    _config_path(args.config), config_descriptor, current, mode
                )
        except (InstallError, OSError) as exc:
            if legacy_backup is not None:
                _restore_directory(destination, legacy_backup)
            if state_backup.exists() and not state_path.exists():
                os.replace(state_backup, state_path)
            if isinstance(exc, InstallError):
                raise
            raise InstallError(
                "finalize_failed",
                "The staged migration could not be finalized atomically.",
            ) from exc
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
        if legacy_backup is not None:
            shutil.rmtree(legacy_backup)
        shutil.rmtree(state_backup)
    return _result(mode="apply", status="finalized", aliases=aliases)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    migrate = subparsers.add_parser("migrate")
    recover_migrate = subparsers.add_parser("recover-migrate")
    restore = subparsers.add_parser("restore")
    rollback = subparsers.add_parser("rollback")
    finalize = subparsers.add_parser("finalize")
    for subparser in (install, migrate, recover_migrate):
        subparser.add_argument(
            "--alias",
            action="append",
            required=True,
            help="One final lowercase Codex MCP alias; supply exactly twice.",
        )
    recover_migrate.add_argument(
        "--expected-config-sha256",
        help="Required on apply; must match the reviewed dry-run preimage digest.",
    )
    for subparser in (install, migrate, recover_migrate, restore, rollback, finalize):
        subparser.add_argument("--skills-root")
        subparser.add_argument("--config")
        subparser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            payload = _install(args)
        elif args.command == "migrate":
            payload = _migrate(args)
        elif args.command == "recover-migrate":
            payload = _recover_migrate(args)
        elif args.command in {"restore", "rollback"}:
            payload = _restore(args)
        else:
            payload = _finalize(args)
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
