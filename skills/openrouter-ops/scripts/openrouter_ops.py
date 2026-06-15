#!/usr/bin/env python3
"""Safe OpenRouter provider operations with 1Password handoff."""

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


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


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


def ensure_op_available() -> str:
    op_path = shutil.which("op")
    if not op_path:
        raise CliError("1Password CLI 'op' was not found on PATH")
    return op_path


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


def metadata_notes(metadata: dict[str, Any]) -> str:
    lines = [
        "OpenRouter runtime API key.",
        "Metadata below is non-secret and was captured when the key was stored.",
        "",
    ]
    for key in sorted(metadata):
        value = metadata[key]
        if value is None:
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def date_field_from_iso(value: str | None) -> str:
    if not value:
        return ""
    try:
        normalized = normalize_utc_timestamp(value)
        return normalized[:10]
    except Exception:
        return ""


def build_api_credential_item(
    *,
    title: str,
    credential: str,
    username: str,
    expires_at: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "title": title,
        "category": "API_CREDENTIAL",
        "fields": [
            {
                "id": "notesPlain",
                "type": "STRING",
                "purpose": "NOTES",
                "label": "notesPlain",
                "value": metadata_notes(metadata),
            },
            {"id": "username", "type": "STRING", "label": "username", "value": username},
            {"id": "credential", "type": "CONCEALED", "label": "credential", "value": credential},
            {"id": "type", "type": "MENU", "label": "type", "value": "OpenRouter API key"},
            {"id": "filename", "type": "STRING", "label": "filename", "value": ""},
            {"id": "validFrom", "type": "DATE", "label": "valid from", "value": utc_now().date().isoformat()},
            {"id": "expires", "type": "DATE", "label": "expires", "value": date_field_from_iso(expires_at)},
            {"id": "hostname", "type": "STRING", "label": "hostname", "value": "openrouter.ai"},
        ],
    }


def run_op_item_create(
    item_template: dict[str, Any],
    *,
    vault: str,
    tags: list[str],
    account: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_op_available()
    cmd = ["op", "item", "create", "--vault", vault, "--format", "json"]
    if account:
        cmd.extend(["--account", account])
    if tags:
        cmd.extend(["--tags", ",".join(tags)])
    if dry_run:
        cmd.append("--dry-run")
    cmd.append("-")

    proc = subprocess.run(
        cmd,
        input=json.dumps(item_template),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise CliError(
            "1Password item create failed",
            {
                "returncode": proc.returncode,
                "stderr": redact(proc.stderr.strip()),
            },
        )
    if not proc.stdout.strip():
        return {"created": not dry_run}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"created": not dry_run, "stdout": redact(proc.stdout.strip())}


def preflight_store(args: argparse.Namespace) -> None:
    fake_template = build_api_credential_item(
        title=args.op_item,
        credential="OPENROUTER_RUNTIME_KEY_REDACTED",
        username=args.name,
        expires_at=expires_from_args(args),
        metadata={"dry_run": True, "source": "openrouter-ops-preflight"},
    )
    run_op_item_create(
        fake_template,
        vault=args.op_vault,
        tags=parse_tags(args.op_tags),
        account=args.op_account,
        dry_run=True,
    )


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


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
                "operation": "create-key",
                "request": {"method": "POST", "path": "/keys", "body": payload},
                "workspace_resolution": workspace_resolution,
                "store_target": {
                    "vault": args.op_vault,
                    "item": args.op_item,
                    "tags": parse_tags(args.op_tags),
                },
                "next_step": "Run again with --live under op run after reviewing this payload.",
            }
        )
        return 0

    preflight_store(args)
    response = api_request("POST", "/keys", token, api_base=args.api_base, body=payload, timeout=args.timeout)
    runtime_key = extract_created_key(response)
    metadata = created_key_metadata(response, workspace_resolution)
    item_template = build_api_credential_item(
        title=args.op_item,
        credential=runtime_key,
        username=metadata.get("label") or args.name,
        expires_at=metadata.get("expires_at") or payload.get("expires_at"),
        metadata=metadata,
    )

    key_hash = metadata.get("hash")
    try:
        op_result = run_op_item_create(
            item_template,
            vault=args.op_vault,
            tags=parse_tags(args.op_tags),
            account=args.op_account,
            dry_run=False,
        )
    except CliError as exc:
        cleanup: dict[str, Any] = {"attempted": False}
        if key_hash:
            cleanup["attempted"] = True
            try:
                cleanup["response"] = redact(
                    api_request(
                        "DELETE",
                        f"/keys/{urllib.parse.quote(str(key_hash), safe='')}",
                        token,
                        api_base=args.api_base,
                        body={},
                        timeout=args.timeout,
                    )
                )
                cleanup["deleted"] = True
            except CliError as cleanup_exc:
                cleanup["deleted"] = False
                cleanup["error"] = str(cleanup_exc)
                cleanup["details"] = getattr(cleanup_exc, "details", {})
        raise CliError(
            "OpenRouter key was created but 1Password storage failed; cleanup was attempted without printing the key",
            {"store_error": str(exc), "store_details": exc.details, "key_hash": key_hash, "cleanup": cleanup},
        ) from exc

    print_json(
        {
            "ok": True,
            "live": True,
            "operation": "create-key",
            "openrouter_key": redact({"data": response.get("data")}),
            "one_password": {
                "stored": True,
                "vault": args.op_vault,
                "item": args.op_item,
                "id": op_result.get("id") if isinstance(op_result, dict) else None,
                "title": op_result.get("title") if isinstance(op_result, dict) else args.op_item,
            },
        }
    )
    return 0


def cmd_store_runtime_key(args: argparse.Namespace) -> int:
    runtime_key = "" if not args.live else secret_from_env(args.runtime_env, "OpenRouter runtime key")
    metadata = {
        "hash": args.hash,
        "label": args.label,
        "name": args.label,
        "workspace_id": args.workspace_id,
        "limit": args.limit,
        "limit_reset": args.limit_reset if args.limit_reset != "omit" else None,
        "expires_at": normalize_utc_timestamp(args.expires_at) if args.expires_at else None,
        "source": "manual-store-runtime-key",
    }
    if not args.live:
        print_json(
            {
                "ok": True,
                "live": False,
                "operation": "store-runtime-key",
                "runtime_env": args.runtime_env,
                "store_target": {"vault": args.op_vault, "item": args.op_item, "tags": parse_tags(args.op_tags)},
                "metadata": metadata,
                "next_step": "Set the runtime key as a secret reference in the runtime env and run again with --live under op run.",
            }
        )
        return 0

    item_template = build_api_credential_item(
        title=args.op_item,
        credential=runtime_key,
        username=args.label or args.hash or "openrouter-runtime-key",
        expires_at=metadata.get("expires_at"),
        metadata=metadata,
    )
    op_result = run_op_item_create(
        item_template,
        vault=args.op_vault,
        tags=parse_tags(args.op_tags),
        account=args.op_account,
        dry_run=False,
    )
    print_json(
        {
            "ok": True,
            "live": True,
            "operation": "store-runtime-key",
            "one_password": {
                "stored": True,
                "vault": args.op_vault,
                "item": args.op_item,
                "id": op_result.get("id") if isinstance(op_result, dict) else None,
            },
        }
    )
    return 0


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


def cmd_run_with_key(args: argparse.Namespace) -> int:
    ensure_op_available()
    if not args.key_ref.startswith(SECRET_REF_PREFIX):
        raise CliError("--key-ref must be a 1Password secret reference starting with op://")
    target_command = args.target_command
    if target_command[:1] == ["--"]:
        target_command = target_command[1:]
    if not target_command:
        raise CliError("run-with-key requires a command after --")
    env = os.environ.copy()
    env[args.env_name] = args.key_ref
    cmd = ["op", "run", "--", *target_command]
    return subprocess.run(cmd, env=env, check=False).returncode


def add_common_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-base", default=API_BASE_DEFAULT)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--management-env", default=MANAGEMENT_ENV_DEFAULT)


def add_store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--op-vault", required=True)
    parser.add_argument("--op-item", required=True)
    parser.add_argument("--op-tags", default="openrouter,api-key")
    parser.add_argument("--op-account")


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

    create_key = subparsers.add_parser("create-key", help="Create and immediately store an OpenRouter runtime key")
    add_common_api_args(create_key)
    add_store_args(create_key)
    create_key.add_argument("--live", action="store_true", help="Perform the OpenRouter and 1Password mutations")
    create_key.add_argument("--name", required=True)
    create_key.add_argument("--workspace", help="Workspace UUID or slug")
    create_key.add_argument("--limit", type=float)
    create_key.add_argument("--limit-reset", choices=["daily", "weekly", "monthly", "none", "omit"], default="omit")
    create_key.add_argument("--expires-at")
    create_key.add_argument("--expires-in-days", type=int)
    create_key.add_argument("--include-byok-in-limit", action="store_true")
    create_key.set_defaults(func=cmd_create_key)

    store_runtime = subparsers.add_parser("store-runtime-key", help="Store an existing runtime key from env in 1Password")
    add_store_args(store_runtime)
    store_runtime.add_argument("--live", action="store_true")
    store_runtime.add_argument("--runtime-env", default=RUNTIME_ENV_DEFAULT)
    store_runtime.add_argument("--hash")
    store_runtime.add_argument("--label")
    store_runtime.add_argument("--workspace-id")
    store_runtime.add_argument("--limit", type=float)
    store_runtime.add_argument("--limit-reset", choices=["daily", "weekly", "monthly", "none", "omit"], default="omit")
    store_runtime.add_argument("--expires-at")
    store_runtime.set_defaults(func=cmd_store_runtime_key)

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

    run_with_key = subparsers.add_parser("run-with-key", help="Run a command with OPENROUTER_API_KEY from 1Password")
    run_with_key.add_argument("--key-ref", required=True)
    run_with_key.add_argument("--env-name", default=RUNTIME_ENV_DEFAULT)
    run_with_key.add_argument("target_command", nargs=argparse.REMAINDER)
    run_with_key.set_defaults(func=cmd_run_with_key)

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
