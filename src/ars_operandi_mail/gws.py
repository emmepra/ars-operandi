from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .config import GwsAccountConfig


METADATA_HEADERS = ("From", "To", "Subject", "Date")
DEFAULT_QUERY = "-in:spam -in:trash"
MAX_RESULTS = 100
MAX_QUERY_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 1000
MAX_MESSAGE_ID_LENGTH = 256
NONINTERACTIVE_TIMEOUT_SECONDS = 15
KEYRING_BACKEND = "keyring"
FORBIDDEN_QUERY_SELECTOR_PATTERNS = (
    r"\bOR\b",
    r"[{}|]",
    r"\bin:anywhere\b",
    r"\b(?:older_than|newer_than):",
)

PROFILE_METHOD = "users.getProfile"
MESSAGE_LIST_METHOD = "users.messages.list"
MESSAGE_GET_METHOD = "users.messages.get"
ALLOWED_GMAIL_METHODS = frozenset(
    {PROFILE_METHOD, MESSAGE_LIST_METHOD, MESSAGE_GET_METHOD}
)
FORBIDDEN_CREDENTIAL_ENV_VARS = frozenset(
    {
        "GOOGLE_WORKSPACE_CLI_TOKEN",
        "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GHA_CREDS_PATH",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
    }
)
SAFE_SUBPROCESS_ENV_VARS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
    }
)
SELECTED_GWS_ENV_VARS = frozenset(
    {"GOOGLE_WORKSPACE_CLI_CONFIG_DIR", "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"}
)


class GwsMailError(RuntimeError):
    """Raised when the fail-closed Gmail runtime cannot complete."""


class GwsMailPolicyError(GwsMailError):
    """Raised before subprocess execution for a forbidden Gmail request."""


class GwsMailIdentityError(GwsMailError):
    """Raised when the selected Gmail identity cannot be proven exactly."""


@dataclass(frozen=True)
class GmailReadRequest:
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class IdentityCheck:
    alias: str
    matched: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MessageMetadata:
    id: str
    sender: str
    to: str
    subject: str
    date: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "from": self.sender,
            "to": self.to,
            "subject": self.subject,
            "date": self.date,
        }


@dataclass(frozen=True)
class OnboardingProbe:
    alias: str
    identity_verified: bool
    result_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class GwsMailCommandRunner:
    """Dedicated Gmail runner with no generic service or passthrough surface."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.environ.get("GWS_BIN", "gws")

    def run_read(self, request: GmailReadRequest, *, env: Mapping[str, str]) -> Any:
        validate_gmail_request(request)
        args = [
            "gmail",
            *_method_segments(request.method),
            "--params",
            _json_params(dict(request.params)),
            "--format",
            "json",
        ]
        stdout = self._run(args, env=env)
        if not stdout.strip():
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GwsMailError("gws returned an invalid response.") from exc

    def auth(self, *, env: Mapping[str, str]) -> None:
        self._run_interactive(
            ["auth", "login", "--readonly", "--services", "gmail"], env=env
        )

    def status(self, *, env: Mapping[str, str]) -> bool:
        self._run(["auth", "status"], env=env)
        return True

    def _run(self, args: list[str], *, env: Mapping[str, str]) -> str:
        clean_env = validate_selected_environment(env)
        executable = shutil.which(self.executable, path=os.environ.get("PATH"))
        if not executable:
            raise GwsMailError("gws executable is unavailable.")
        try:
            completed = subprocess.run(
                [executable, *args],
                env=clean_env,
                text=True,
                capture_output=True,
                check=False,
                timeout=NONINTERACTIVE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise GwsMailError("gws operation timed out.") from None
        except OSError as exc:
            raise GwsMailError("gws could not be started.") from exc
        if completed.returncode != 0:
            raise GwsMailError(
                f"gws operation failed (exit code {completed.returncode})."
            )
        return completed.stdout or ""

    def _run_interactive(self, args: list[str], *, env: Mapping[str, str]) -> None:
        clean_env = validate_selected_environment(env)
        executable = shutil.which(self.executable, path=os.environ.get("PATH"))
        if not executable:
            raise GwsMailError("gws executable is unavailable.")
        try:
            completed = subprocess.run([executable, *args], env=clean_env, check=False)
        except OSError as exc:
            raise GwsMailError("gws could not be started.") from exc
        if completed.returncode != 0:
            raise GwsMailError(
                f"gws operation failed (exit code {completed.returncode})."
            )


class GwsMailClient:
    def __init__(self, account: GwsAccountConfig, runner: Any | None = None) -> None:
        self.account = account
        self.runner = runner or GwsMailCommandRunner()

    def auth(self) -> None:
        self.account.config_dir.mkdir(parents=True, exist_ok=True)
        self.runner.auth(env=self._env())
        self.verify_identity()

    def status(self) -> bool:
        if not self.runner.status(env=self._env()):
            raise GwsMailError("gws authentication is unavailable.")
        self.verify_identity()
        return True

    def verify_identity(self) -> IdentityCheck:
        response = self.runner.run_read(
            GmailReadRequest(PROFILE_METHOD, {"userId": "me"}), env=self._env()
        )
        if not isinstance(response, dict):
            raise GwsMailIdentityError("Gmail identity response is invalid.")
        if response.get("emailAddress") != self.account.email:
            raise GwsMailIdentityError("Gmail profile identity mismatch.")
        return IdentityCheck(alias=self.account.alias)

    def search(
        self, *, query: str, after: str, before: str, max_results: int
    ) -> tuple[MessageMetadata, ...]:
        self._require_verified_binding()
        bounded_query = build_query(query=query, after=after, before=before)
        self.verify_identity()
        ids = self._list_message_ids(query=bounded_query, max_results=max_results)
        return tuple(self.get_metadata(message_id) for message_id in ids)

    def get_metadata(self, message_id: str) -> MessageMetadata:
        self._require_verified_binding()
        _validate_message_id(message_id)
        self.verify_identity()
        response = self.runner.run_read(
            GmailReadRequest(
                MESSAGE_GET_METHOD,
                {
                    "userId": "me",
                    "id": message_id,
                    "format": "metadata",
                    "metadataHeaders": list(METADATA_HEADERS),
                },
            ),
            env=self._env(),
        )
        return sanitize_message_metadata(response, expected_id=message_id)

    def onboarding_probe(
        self, *, query: str, after: str, before: str
    ) -> OnboardingProbe:
        bounded_query = build_query(query=query, after=after, before=before)
        self.verify_identity()
        ids = self._list_message_ids(query=bounded_query, max_results=1)
        return OnboardingProbe(self.account.alias, True, len(ids))

    def _list_message_ids(self, *, query: str, max_results: int) -> tuple[str, ...]:
        request = GmailReadRequest(
            MESSAGE_LIST_METHOD,
            {"userId": "me", "q": query, "maxResults": max_results},
        )
        response = self.runner.run_read(request, env=self._env())
        if not isinstance(response, dict):
            raise GwsMailError("Gmail list response is invalid.")
        messages = response.get("messages", [])
        if messages is None:
            messages = []
        if not isinstance(messages, list):
            raise GwsMailError("Gmail list response is invalid.")
        ids: list[str] = []
        for item in messages[:max_results]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise GwsMailError("Gmail list response is invalid.")
            message_id = str(item["id"])
            _validate_message_id(message_id)
            ids.append(message_id)
        return tuple(ids)

    def _require_verified_binding(self) -> None:
        if not self.account.is_verified:
            raise GwsMailPolicyError(
                "Normal Gmail reads require a verified mail binding."
            )

    def _env(self) -> dict[str, str]:
        return clean_gws_environment(self.account.config_dir)


def validate_gmail_request(request: GmailReadRequest) -> None:
    if request.method not in ALLOWED_GMAIL_METHODS:
        raise GwsMailPolicyError("Gmail method is not allowlisted.")
    params = dict(request.params)
    if request.method == PROFILE_METHOD:
        if params != {"userId": "me"}:
            raise GwsMailPolicyError(
                "Gmail profile request parameters are not allowed."
            )
        return
    if request.method == MESSAGE_LIST_METHOD:
        if set(params) != {"userId", "q", "maxResults"} or params.get("userId") != "me":
            raise GwsMailPolicyError("Gmail list request parameters are not allowed.")
        query = params.get("q")
        maximum = params.get("maxResults")
        if not isinstance(query, str):
            raise GwsMailPolicyError("Gmail list query is invalid.")
        validate_bounded_query(query)
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or not 1 <= maximum <= MAX_RESULTS
        ):
            raise GwsMailPolicyError("Gmail list maxResults must be finite.")
        return
    if set(params) != {"userId", "id", "format", "metadataHeaders"}:
        raise GwsMailPolicyError("Gmail metadata request parameters are not allowed.")
    if params.get("userId") != "me" or params.get("format") != "metadata":
        raise GwsMailPolicyError("Gmail message reads must use metadata format.")
    if params.get("metadataHeaders") != list(METADATA_HEADERS):
        raise GwsMailPolicyError("Gmail metadata headers are not allowlisted.")
    message_id = params.get("id")
    if not isinstance(message_id, str):
        raise GwsMailPolicyError("Gmail message id is invalid.")
    _validate_message_id(message_id)


def build_query(*, query: str, after: str, before: str) -> str:
    start = _parse_date_bound(after, option="after")
    end = _parse_date_bound(before, option="before")
    if start >= end:
        raise GwsMailPolicyError("Gmail query start must be before its end.")
    selector = query.strip()
    if any(ord(character) < 32 for character in selector):
        raise GwsMailPolicyError("Gmail query contains control characters.")
    if _query_uses_forbidden_selector(selector):
        raise GwsMailPolicyError(
            "Gmail query contains an operator that can escape the required window."
        )
    bounded = " ".join(
        part
        for part in (
            DEFAULT_QUERY,
            selector,
            f"after:{start:%Y/%m/%d}",
            f"before:{end:%Y/%m/%d}",
        )
        if part
    )
    validate_bounded_query(bounded)
    return bounded


def validate_bounded_query(query: str) -> None:
    if not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise GwsMailPolicyError("Gmail query must be non-empty and bounded.")
    if any(ord(character) < 32 for character in query):
        raise GwsMailPolicyError("Gmail query contains control characters.")
    if _query_uses_forbidden_selector(query):
        raise GwsMailPolicyError(
            "Gmail query contains an operator that can escape the required window."
        )
    starts = [
        token
        for token in query.split()
        if re.fullmatch(r"after:\d{4}/\d{2}/\d{2}", token)
    ]
    ends = [
        token
        for token in query.split()
        if re.fullmatch(r"before:\d{4}/\d{2}/\d{2}", token)
    ]
    if len(starts) != 1 or len(ends) != 1:
        raise GwsMailPolicyError(
            "Gmail query must include one explicit start and end date."
        )
    start = _parse_gmail_date_token(starts[0], prefix="after:")
    end = _parse_gmail_date_token(ends[0], prefix="before:")
    if start >= end:
        raise GwsMailPolicyError("Gmail query window is invalid.")


def sanitize_message_metadata(response: object, *, expected_id: str) -> MessageMetadata:
    if not isinstance(response, dict) or response.get("id") != expected_id:
        raise GwsMailError("Gmail metadata response is invalid.")
    payload = response.get("payload")
    if not isinstance(payload, dict) or not isinstance(
        payload.get("headers", []), list
    ):
        raise GwsMailError("Gmail metadata response is invalid.")
    values = {header: "" for header in METADATA_HEADERS}
    for item in payload.get("headers", []):
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        canonical = next(
            (
                allowed
                for allowed in METADATA_HEADERS
                if allowed.casefold() == name.casefold()
            ),
            None,
        )
        if canonical is not None and not values[canonical]:
            values[canonical] = _sanitize_header_value(value)
    return MessageMetadata(
        expected_id,
        values["From"],
        values["To"],
        values["Subject"],
        values["Date"],
    )


def clean_gws_environment(config_dir: Path) -> dict[str, str]:
    reject_ambient_credential_overrides()
    clean = {
        name: value
        for name in SAFE_SUBPROCESS_ENV_VARS
        if (value := os.environ.get(name)) is not None
    }
    clean["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    clean["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = KEYRING_BACKEND
    return clean


def reject_ambient_credential_overrides() -> None:
    if any(name in os.environ for name in FORBIDDEN_CREDENTIAL_ENV_VARS):
        raise GwsMailPolicyError("Ambient Google credential override is forbidden.")


def validate_selected_environment(env: Mapping[str, str]) -> dict[str, str]:
    reject_ambient_credential_overrides()
    allowed = SAFE_SUBPROCESS_ENV_VARS | SELECTED_GWS_ENV_VARS
    if set(env) - allowed:
        raise GwsMailPolicyError("Unexpected environment variable for gws.")
    if not env.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR"):
        raise GwsMailPolicyError("Selected gws config directory is missing.")
    if env.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND") != KEYRING_BACKEND:
        raise GwsMailPolicyError("gws keyring backend is not enforced.")
    return dict(env)


def _query_uses_forbidden_selector(query: str) -> bool:
    return any(
        re.search(pattern, query, flags=re.IGNORECASE)
        for pattern in FORBIDDEN_QUERY_SELECTOR_PATTERNS
    )


def _method_segments(method: str) -> tuple[str, ...]:
    mapping = {
        PROFILE_METHOD: ("users", "getProfile"),
        MESSAGE_LIST_METHOD: ("users", "messages", "list"),
        MESSAGE_GET_METHOD: ("users", "messages", "get"),
    }
    try:
        return mapping[method]
    except KeyError as exc:
        raise GwsMailPolicyError("Gmail method is not allowlisted.") from exc


def _validate_message_id(message_id: str) -> None:
    if (
        not message_id
        or len(message_id) > MAX_MESSAGE_ID_LENGTH
        or re.fullmatch(r"[A-Za-z0-9_-]+", message_id) is None
    ):
        raise GwsMailPolicyError("Gmail message id is invalid.")


def _parse_date_bound(value: str, *, option: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError) as exc:
        raise GwsMailPolicyError(f"Gmail {option} bound must use YYYY-MM-DD.") from exc


def _parse_gmail_date_token(token: str, *, prefix: str) -> date:
    try:
        return datetime.strptime(token.removeprefix(prefix), "%Y/%m/%d").date()
    except ValueError as exc:
        raise GwsMailPolicyError("Gmail query date bound is invalid.") from exc


def _sanitize_header_value(value: str) -> str:
    return " ".join(value.split())[:MAX_HEADER_VALUE_LENGTH]


def _json_params(params: dict[str, object]) -> str:
    return json.dumps(params, separators=(",", ":"), ensure_ascii=True)
