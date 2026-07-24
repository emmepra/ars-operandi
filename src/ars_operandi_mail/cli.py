from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .mail_content import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    DEFAULT_CONTENT_MAX_BYTES,
)
from .service import MailRuntime, build_mcp, safe_error_payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed bounded read-only mail access owned by Ars Operandi."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in (
        "accounts",
        "status",
        "auth",
        "onboarding-verify",
        "search",
        "metadata",
        "content",
        "attachment",
        "serve",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--project-index", type=Path, required=True)
        command.add_argument("--config-root", type=Path, required=True)
        if name in {
            "status",
            "auth",
            "onboarding-verify",
            "search",
            "metadata",
            "content",
            "attachment",
        }:
            command.add_argument("--account")
        if name in {
            "status",
            "onboarding-verify",
            "search",
            "metadata",
            "content",
            "attachment",
        }:
            command.add_argument("--project")
        if name in {"onboarding-verify", "search"}:
            command.add_argument("--after", required=True)
            command.add_argument("--before", required=True)
            command.add_argument("--query", default="")
        if name == "search":
            command.add_argument("--max-results", type=int, default=10)
        if name in {"metadata", "content", "attachment"}:
            command.add_argument("--message", required=True)
        if name == "content":
            command.add_argument(
                "--max-bytes", type=int, default=DEFAULT_CONTENT_MAX_BYTES
            )
        if name == "attachment":
            command.add_argument("--attachment", required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument(
                "--max-bytes", type=int, default=DEFAULT_ATTACHMENT_MAX_BYTES
            )
        if name not in {"serve", "auth"}:
            command.add_argument("--json", action="store_true")
    return parser


def _runtime(args: argparse.Namespace) -> MailRuntime:
    return MailRuntime(
        project_index=args.project_index,
        config_root=args.config_root,
    )


def _execute(runtime: MailRuntime, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "accounts":
        return runtime.accounts()
    if args.command == "status":
        return runtime.status(account=args.account, project=args.project)
    if args.command == "auth":
        if not args.account:
            raise ValueError("Select exactly one mail profile alias for GWS auth.")
        return runtime.auth_gws(account=args.account)
    if args.command == "onboarding-verify":
        return runtime.onboarding(
            account=args.account,
            project=args.project,
            after=args.after,
            before=args.before,
            query=args.query,
        )
    if args.command == "search":
        return runtime.search(
            account=args.account,
            project=args.project,
            after=args.after,
            before=args.before,
            query=args.query,
            max_results=args.max_results,
        )
    if args.command == "content":
        return runtime.content(
            account=args.account,
            project=args.project,
            message_id=args.message,
            max_bytes=args.max_bytes,
        )
    if args.command == "attachment":
        return runtime.attachment(
            account=args.account,
            project=args.project,
            message_id=args.message,
            attachment_id=args.attachment,
            output_path=args.output,
            max_bytes=args.max_bytes,
        )
    return runtime.metadata(
        account=args.account,
        project=args.project,
        message_id=args.message,
    )


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime: MailRuntime | None = None
    try:
        runtime = _runtime(args)
        if args.command == "serve":
            build_mcp(runtime).run(transport="stdio")
            return 0
        payload = _execute(runtime, args)
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as exc:
        error = (
            safe_error_payload(runtime, exc)
            if runtime is not None
            else {
                "code": "mailctl_failed",
                "message": "The mail operation failed without exposing runtime details.",
            }
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
