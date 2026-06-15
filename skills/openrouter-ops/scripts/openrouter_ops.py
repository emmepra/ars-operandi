#!/usr/bin/env python3
"""Safe OpenRouter provider operations with ephemeral runtime keys."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_BASE_DEFAULT = "https://openrouter.ai/api/v1"
MANAGEMENT_ENV_DEFAULT = "OPENROUTER_MANAGEMENT_KEY"
RUNTIME_ENV_DEFAULT = "OPENROUTER_API_KEY"
SECRET_REF_PREFIX = "op://"
OPENROUTER_KEY_RE = re.compile(r"sk-or-[A-Za-z0-9._-]+")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class CliError(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None, code: int = 1):
        super().__init__(message)
        self.details = details or {}
        self.code = code


class OpenRouterError(CliError):
    pass


def print_json(data: Any, *, stream: Any = sys.stdout) -> None:
    json.dump(data, stream, indent=2, sort_keys=True)
    stream.write("\n")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {
                "key",
                "api_key",
                "token",
                "authorization",
                "credential",
                "password",
                "secret",
                "creator_user_id",
                "created_by",
            }:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return OPENROUTER_KEY_RE.sub("<redacted-openrouter-key>", value)
    return value


def redact_text(value: str, *extra_secrets: str) -> str:
    redacted = OPENROUTER_KEY_RE.sub("<redacted-openrouter-key>", value)
    for secret in extra_secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted-openrouter-key>")
    return redacted


def secret_from_env(env_name: str, purpose: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise CliError(
            f"Missing {purpose} in {env_name}. Set {env_name}=op://... and run the command through op run."
        )
    if value.startswith(SECRET_REF_PREFIX):
        raise CliError(
            f"{env_name} still contains a 1Password secret reference. Wrap the command with op run."
        )
    return value


def api_request(
    method: str,
    path: str,
    token: str,
    *,
    api_base: str = API_BASE_DEFAULT,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    base = api_base.rstrip("/")
    url = f"{base}{path}"
    if query:
        clean_query = {key: value for key, value in query.items() if value is not None}
        if clean_query:
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"

    encoded_body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        encoded_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=encoded_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            if not payload:
                return {}
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        raise OpenRouterError(
            f"OpenRouter request failed with HTTP {exc.code}",
            {"status": exc.code, "body": redact(parsed), "path": path},
        ) from exc
    except urllib.error.URLError as exc:
        raise OpenRouterError(f"OpenRouter request failed: {exc.reason}", {"path": path}) from exc


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_utc_timestamp(raw: str) -> str:
    value = raw.strip()
    if value.endswith("Z"):
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    else:
        parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CliError("expires_at must include UTC timezone, for example 2026-06-22T00:00:00Z")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expires_from_args(args: argparse.Namespace) -> str | None:
    if args.expires_at and args.expires_in_days is not None:
        raise CliError("Use either --expires-at or --expires-in-days, not both")
    if args.expires_at:
        return normalize_utc_timestamp(args.expires_at)
    if args.expires_in_days is not None:
        if args.expires_in_days <= 0:
            raise CliError("--expires-in-days must be positive")
        expires = utc_now() + dt.timedelta(days=args.expires_in_days)
        return expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return None


def workspace_is_uuid(workspace: str) -> bool:
    return bool(UUID_RE.fullmatch(workspace))


def resolve_workspace_id(
    workspace: str | None,
    token: str,
    *,
    api_base: str,
    timeout: float,
    dry_run: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    if not workspace:
        return None, None
    if workspace_is_uuid(workspace):
        return workspace, {"input": workspace, "resolved_by": "uuid"}
    if dry_run:
        return None, {"input": workspace, "resolved_by": "pending-live-slug-resolution"}

    encoded = urllib.parse.quote(workspace, safe="")
    response = api_request("GET", f"/workspaces/{encoded}", token, api_base=api_base, timeout=timeout)
    data = response.get("data") or {}
    workspace_id = data.get("id")
    if not workspace_id:
        raise CliError("Workspace lookup did not return an id", {"workspace": workspace, "response": redact(response)})
    return workspace_id, {"input": workspace, "resolved_by": "slug", "workspace": redact(data)}


def build_create_payload(args: argparse.Namespace, workspace_id: str | None) -> dict[str, Any]:
    if args.limit is not None and args.limit < 0:
        raise CliError("--limit must be zero or positive")

    payload: dict[str, Any] = {
        "name": args.name,
        "include_byok_in_limit": bool(args.include_byok_in_limit),
    }
    expires_at = expires_from_args(args)
    if expires_at:
        payload["expires_at"] = expires_at
    if args.limit is not None:
        payload["limit"] = args.limit
    if args.limit_reset != "omit":
        payload["limit_reset"] = None if args.limit_reset == "none" else args.limit_reset
    if workspace_id:
        payload["workspace_id"] = workspace_id
    return payload


def extract_created_key(response: dict[str, Any]) -> str:
    key = response.get("key")
    if isinstance(key, str) and key:
        return key
    nested = response.get("data") or {}
    key = nested.get("key")
    if isinstance(key, str) and key:
        return key
    raise CliError("OpenRouter create-key response did not include a one-time key")


def created_key_metadata(response: dict[str, Any], workspace_resolution: dict[str, Any] | None) -> dict[str, Any]:
    data = response.get("data") or {}
    metadata = {
        "hash": data.get("hash"),
        "label": data.get("label"),
        "name": data.get("name"),
        "workspace_id": data.get("workspace_id"),
        "limit": data.get("limit"),
        "limit_remaining": data.get("limit_remaining"),
        "limit_reset": data.get("limit_reset"),
        "expires_at": data.get("expires_at"),
        "created_at": data.get("created_at"),
        "include_byok_in_limit": data.get("include_byok_in_limit"),
    }
    if workspace_resolution:
        metadata["workspace_input"] = workspace_resolution.get("input")
        metadata["workspace_resolved_by"] = workspace_resolution.get("resolved_by")
    return metadata


def target_command_from_args(args: argparse.Namespace) -> list[str]:
    target_command = list(args.target_command)
    if target_command[:1] == ["--"]:
        target_command = target_command[1:]
    if not target_command:
        raise CliError(f"{args.command} requires a command after --")
    return target_command


def build_child_env(
    base_env: dict[str, str],
    *,
    runtime_key: str,
    runtime_env_name: str,
    management_env_name: str,
) -> dict[str, str]:
    child_env = dict(base_env)
    child_env.pop(management_env_name, None)
    child_env[runtime_env_name] = runtime_key
    return child_env


def delete_runtime_key(
    *,
    token: str,
    key_hash: str | None,
    api_base: str,
    timeout: float,
) -> dict[str, Any]:
    if not key_hash:
        return {"attempted": False, "deleted": False, "reason": "missing-key-hash"}
    try:
        response = api_request(
            "DELETE",
            f"/keys/{urllib.parse.quote(str(key_hash), safe='')}",
            token,
            api_base=api_base,
            body={},
            timeout=timeout,
        )
        return {"attempted": True, "deleted": True, "response": redact(response)}
    except CliError as exc:
        return {
            "attempted": True,
            "deleted": False,
            "error": str(exc),
            "details": redact(getattr(exc, "details", {})),
        }


def emit_child_output(proc: subprocess.CompletedProcess[str], runtime_key: str) -> None:
    if proc.stdout:
        sys.stdout.write(redact_text(proc.stdout, runtime_key))
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(redact_text(proc.stderr, runtime_key))
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")


def cmd_preflight(args: argparse.Namespace) -> int:
    op_path = shutil.which("op")
    env_value = os.environ.get(args.management_env)
    result: dict[str, Any] = {
        "ok": True,
        "op_available": bool(op_path),
        "op_path": op_path,
        "management_env": args.management_env,
        "management_env_present": bool(env_value),
        "management_env_is_secret_ref": bool(env_value and env_value.startswith(SECRET_REF_PREFIX)),
    }
    if args.check_api:
        token = secret_from_env(args.management_env, "OpenRouter management key")
        response = api_request("GET", "/key", token, api_base=args.api_base, timeout=args.timeout)
        result["openrouter_key"] = redact(response)
    print_json(result)
    return 0


def cmd_workspaces(args: argparse.Namespace) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    response = api_request(
        "GET",
        "/workspaces",
        token,
        api_base=args.api_base,
        query={"offset": args.offset, "limit": args.limit},
        timeout=args.timeout,
    )
    print_json({"ok": True, "workspaces": redact(response)})
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    encoded = urllib.parse.quote(args.workspace, safe="")
    response = api_request("GET", f"/workspaces/{encoded}", token, api_base=args.api_base, timeout=args.timeout)
    print_json({"ok": True, "workspace": redact(response)})
    return 0


def cmd_list_keys(args: argparse.Namespace) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    workspace_id, workspace_resolution = resolve_workspace_id(
        args.workspace,
        token,
        api_base=args.api_base,
        timeout=args.timeout,
        dry_run=False,
    )
    response = api_request(
        "GET",
        "/keys",
        token,
        api_base=args.api_base,
        query={
            "include_disabled": "true" if args.include_disabled else None,
            "offset": args.offset,
            "workspace_id": workspace_id,
        },
        timeout=args.timeout,
    )
    print_json({"ok": True, "workspace_resolution": workspace_resolution, "keys": redact(response)})
    return 0


def cmd_current_key(args: argparse.Namespace) -> int:
    token = secret_from_env(args.key_env, "OpenRouter API key")
    response = api_request("GET", "/key", token, api_base=args.api_base, timeout=args.timeout)
    print_json({"ok": True, "current_key": redact(response)})
    return 0


def cmd_credits(args: argparse.Namespace) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    response = api_request("GET", "/credits", token, api_base=args.api_base, timeout=args.timeout)
    print_json({"ok": True, "credits": redact(response)})
    return 0


def cmd_create_key(args: argparse.Namespace) -> int:
    if args.live:
        raise CliError(
            "Standalone create-key no longer creates runtime keys. Use run-ephemeral --live -- <command> "
            "so the key is injected and then deleted."
        )
    workspace_id, workspace_resolution = resolve_workspace_id(
        args.workspace,
        "",
        api_base=args.api_base,
        timeout=args.timeout,
        dry_run=True,
    )
    payload = build_create_payload(args, workspace_id)

    print_json(
        {
            "ok": True,
            "live": False,
            "operation": "create-key",
            "request": {"method": "POST", "path": "/keys", "body": payload},
            "workspace_resolution": workspace_resolution,
            "next_step": "Use run-ephemeral --live under op run to create, inject, and delete a runtime key.",
        }
    )
    return 0


def cmd_run_ephemeral(args: argparse.Namespace) -> int:
    target_command = target_command_from_args(args)
    dry_run = not args.live
    token = "" if dry_run else secret_from_env(args.management_env, "OpenRouter management key")
    workspace_id, workspace_resolution = resolve_workspace_id(
        args.workspace,
        token,
        api_base=args.api_base,
        timeout=args.timeout,
        dry_run=dry_run,
    )
    payload = build_create_payload(args, workspace_id)

    if dry_run:
        print_json(
            {
                "ok": True,
                "live": False,
                "operation": "run-ephemeral",
                "request": {"method": "POST", "path": "/keys", "body": payload},
                "workspace_resolution": workspace_resolution,
                "env_name": args.env_name,
                "command": target_command,
                "cleanup": {"on_exit": "delete-key"},
                "next_step": "Run again with --live under op run to create, inject, and delete a runtime key.",
            }
        )
        return 0

    response = api_request("POST", "/keys", token, api_base=args.api_base, body=payload, timeout=args.timeout)
    runtime_key = extract_created_key(response)
    metadata = created_key_metadata(response, workspace_resolution)
    key_hash = metadata.get("hash")
    child_env = build_child_env(
        os.environ,
        runtime_key=runtime_key,
        runtime_env_name=args.env_name,
        management_env_name=args.management_env,
    )

    command_returncode = 1
    cleanup: dict[str, Any] = {"attempted": False, "deleted": False}
    try:
        try:
            proc = subprocess.run(
                target_command,
                env=child_env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            command_returncode = proc.returncode
            emit_child_output(proc, runtime_key)
        except OSError as exc:
            command_returncode = 127
            sys.stderr.write(f"openrouter-ops: failed to start command: {exc}\n")
    finally:
        cleanup = delete_runtime_key(
            token=token,
            key_hash=str(key_hash) if key_hash else None,
            api_base=args.api_base,
            timeout=args.timeout,
        )

    summary = {
        "ok": command_returncode == 0 and bool(cleanup.get("deleted")),
        "live": True,
        "operation": "run-ephemeral",
        "openrouter_key": redact({"data": response.get("data")}),
        "env_name": args.env_name,
        "command_returncode": command_returncode,
        "cleanup": cleanup,
    }
    print_json(summary, stream=sys.stderr)
    if command_returncode == 0 and not cleanup.get("deleted"):
        return 2
    return command_returncode


def patch_key_disabled(args: argparse.Namespace, disabled: bool) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    body = {"disabled": disabled}
    if not args.live:
        print_json(
            {
                "ok": True,
                "live": False,
                "operation": "disable-key" if disabled else "enable-key",
                "request": {"method": "PATCH", "path": f"/keys/{args.hash}", "body": body},
                "next_step": "Run again with --live under op run to mutate OpenRouter.",
            }
        )
        return 0
    response = api_request(
        "PATCH",
        f"/keys/{urllib.parse.quote(args.hash, safe='')}",
        token,
        api_base=args.api_base,
        body=body,
        timeout=args.timeout,
    )
    print_json({"ok": True, "live": True, "key": redact(response)})
    return 0


def cmd_disable_key(args: argparse.Namespace) -> int:
    return patch_key_disabled(args, True)


def cmd_enable_key(args: argparse.Namespace) -> int:
    return patch_key_disabled(args, False)


def cmd_delete_key(args: argparse.Namespace) -> int:
    token = secret_from_env(args.management_env, "OpenRouter management key")
    if not args.live:
        print_json(
            {
                "ok": True,
                "live": False,
                "operation": "delete-key",
                "request": {"method": "DELETE", "path": f"/keys/{args.hash}", "body": {}},
                "next_step": "Run again with --live under op run to delete the OpenRouter key.",
            }
        )
        return 0
    response = api_request(
        "DELETE",
        f"/keys/{urllib.parse.quote(args.hash, safe='')}",
        token,
        api_base=args.api_base,
        body={},
        timeout=args.timeout,
    )
    print_json({"ok": True, "live": True, "deleted": redact(response)})
    return 0


def add_common_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-base", default=API_BASE_DEFAULT)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--management-env", default=MANAGEMENT_ENV_DEFAULT)


def add_key_request_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", required=True)
    parser.add_argument("--workspace", help="Workspace UUID or slug")
    parser.add_argument("--limit", type=float)
    parser.add_argument("--limit-reset", choices=["daily", "weekly", "monthly", "none", "omit"], default="omit")
    parser.add_argument("--expires-at")
    parser.add_argument("--expires-in-days", type=int)
    parser.add_argument("--include-byok-in-limit", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="Check local prerequisites without printing secrets")
    add_common_api_args(preflight)
    preflight.add_argument("--check-api", action="store_true")
    preflight.set_defaults(func=cmd_preflight)

    workspaces = subparsers.add_parser("workspaces", help="List OpenRouter workspaces")
    add_common_api_args(workspaces)
    workspaces.add_argument("--offset", type=int, default=0)
    workspaces.add_argument("--limit", type=int, default=100)
    workspaces.set_defaults(func=cmd_workspaces)

    workspace = subparsers.add_parser("workspace", help="Get one workspace by ID or slug")
    add_common_api_args(workspace)
    workspace.add_argument("workspace")
    workspace.set_defaults(func=cmd_workspace)

    list_keys = subparsers.add_parser("list-keys", help="List OpenRouter API keys")
    add_common_api_args(list_keys)
    list_keys.add_argument("--workspace", help="Workspace UUID or slug")
    list_keys.add_argument("--include-disabled", action="store_true")
    list_keys.add_argument("--offset", type=int, default=0)
    list_keys.set_defaults(func=cmd_list_keys)

    current_key = subparsers.add_parser("current-key", help="Inspect a runtime or management key from env")
    current_key.add_argument("--api-base", default=API_BASE_DEFAULT)
    current_key.add_argument("--timeout", type=float, default=30)
    current_key.add_argument("--key-env", default=RUNTIME_ENV_DEFAULT)
    current_key.set_defaults(func=cmd_current_key)

    credits = subparsers.add_parser("credits", help="Get OpenRouter credits with a management key")
    add_common_api_args(credits)
    credits.set_defaults(func=cmd_credits)

    create_key = subparsers.add_parser("create-key", help="Dry-run an OpenRouter runtime key payload")
    add_common_api_args(create_key)
    create_key.add_argument("--live", action="store_true", help="Rejected: use run-ephemeral --live instead")
    add_key_request_args(create_key)
    create_key.set_defaults(func=cmd_create_key)

    run_ephemeral = subparsers.add_parser(
        "run-ephemeral",
        help="Create a runtime key, inject it into a command, then delete it",
    )
    add_common_api_args(run_ephemeral)
    run_ephemeral.add_argument("--live", action="store_true", help="Perform the OpenRouter mutation and run the command")
    add_key_request_args(run_ephemeral)
    run_ephemeral.add_argument("--env-name", default=RUNTIME_ENV_DEFAULT)
    run_ephemeral.add_argument("target_command", nargs=argparse.REMAINDER)
    run_ephemeral.set_defaults(func=cmd_run_ephemeral)

    disable_key = subparsers.add_parser("disable-key", help="Disable an OpenRouter API key by hash")
    add_common_api_args(disable_key)
    disable_key.add_argument("--live", action="store_true")
    disable_key.add_argument("--hash", required=True)
    disable_key.set_defaults(func=cmd_disable_key)

    enable_key = subparsers.add_parser("enable-key", help="Enable an OpenRouter API key by hash")
    add_common_api_args(enable_key)
    enable_key.add_argument("--live", action="store_true")
    enable_key.add_argument("--hash", required=True)
    enable_key.set_defaults(func=cmd_enable_key)

    delete_key = subparsers.add_parser("delete-key", help="Delete an OpenRouter API key by hash")
    add_common_api_args(delete_key)
    delete_key.add_argument("--live", action="store_true")
    delete_key.add_argument("--hash", required=True)
    delete_key.set_defaults(func=cmd_delete_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print_json({"ok": False, "error": str(exc), "details": redact(exc.details)})
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
