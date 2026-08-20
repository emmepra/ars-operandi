from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import base64
import binascii
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .config import GwsAccountConfig
from .mail_content import (
    AttachmentMetadata,
    MailContentPolicyError,
    MessageContent,
    decode_text,
    html_to_text,
    sanitize_content_type,
    sanitize_filename,
    sanitize_html,
    validate_attachment_max_bytes,
    validate_content_max_bytes,
)


METADATA_HEADERS = ("From", "To", "Subject", "Date")
DEFAULT_QUERY = "-in:spam -in:trash"
MAX_RESULTS = 100
MAX_QUERY_LENGTH = 2048
MAX_HEADER_VALUE_LENGTH = 1000
MAX_MESSAGE_ID_LENGTH = 256
NONINTERACTIVE_TIMEOUT_SECONDS = 15
KEYRING_BACKEND = "keyring"
TOKEN_CACHE_FILENAME = "token_cache.json"
ADC_DENIAL_SENTINEL_FILENAME = ".ars-operandi-adc-denied.json"
FALLBACK_CREDENTIAL_ERROR_MESSAGE = (
    "GWS plaintext or ADC fallback credentials are forbidden."
)
OS_ACCOUNT_ERROR_MESSAGE = "The local OS account for gws is invalid."
OS_ACCOUNT_ENV_ERROR_MESSAGE = (
    "The ambient OS account environment for gws is invalid."
)
GWS_REQUIRED_SCOPES = frozenset(
    {
        "openid",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
)
GWS_ECHOED_OIDC_SCOPE_ALIASES = frozenset({"email", "profile"})
FORBIDDEN_QUERY_SELECTOR_PATTERNS = (
    r"\bOR\b",
    r"[{}|]",
    r"\bin:anywhere\b",
    r"\b(?:older_than|newer_than):",
)

PROFILE_METHOD = "users.getProfile"
MESSAGE_LIST_METHOD = "users.messages.list"
MESSAGE_GET_METHOD = "users.messages.get"
ATTACHMENT_GET_METHOD = "users.messages.attachments.get"
ALLOWED_GMAIL_METHODS = frozenset(
    {PROFILE_METHOD, MESSAGE_LIST_METHOD, MESSAGE_GET_METHOD, ATTACHMENT_GET_METHOD}
)
GMAIL_ATTACHMENT_ID_PREFIX = "gma1_"
FORBIDDEN_CREDENTIAL_ENV_VARS = frozenset(
    {
        "GOOGLE_WORKSPACE_CLI_TOKEN",
        "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
        "GOOGLE_WORKSPACE_CLI_CLIENT_ID",
        "GOOGLE_WORKSPACE_CLI_CLIENT_SECRET",
        "GOOGLE_WORKSPACE_CLI_ACCOUNT",
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
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_WORKSPACE_CLI_CONFIG_DIR",
        "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND",
    }
)


class GwsMailError(RuntimeError):
    """Raised when the fail-closed Gmail runtime cannot complete."""


class GwsMailPolicyError(GwsMailError):
    """Raised before subprocess execution for a forbidden Gmail request."""


GWS_AUTH_ERROR_MESSAGES = {
    "gws_auth_status_invalid": "The selected GWS authentication status is invalid.",
    "gws_auth_backend_invalid": (
        "The selected GWS profile is not using the required keyring backend."
    ),
    "gws_auth_source_invalid": (
        "The selected GWS profile is not using the required encrypted OAuth source."
    ),
    "gws_auth_decryption_failed": (
        "The selected GWS encrypted credentials cannot be decrypted."
    ),
    "gws_auth_invalid_client": "The selected GWS OAuth client is invalid.",
    "gws_auth_token_invalid": (
        "The selected GWS OAuth token is unavailable or invalid."
    ),
    "gws_auth_scopes_invalid": (
        "The selected GWS OAuth scopes do not match the required Gmail read-only set."
    ),
}


class GwsMailAuthStatusError(GwsMailError):
    """Stable, sanitized failure derived from structured GWS auth status."""

    def __init__(self, code: str) -> None:
        try:
            message = GWS_AUTH_ERROR_MESSAGES[code]
        except KeyError as exc:  # pragma: no cover - internal closed catalog
            raise ValueError("Unknown GWS authentication error code.") from exc
        self.code = code
        self.message = message
        super().__init__(message)


class GwsMailIdentityError(GwsMailError):
    """Raised when the selected Gmail identity cannot be proven exactly."""

    code = "gws_identity_mismatch"
    message = (
        "The selected GWS profile identity does not match the configured account."
    )

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass(frozen=True)
class GmailReadRequest:
    method: str
    params: Mapping[str, object]


@dataclass(frozen=True)
class GwsAuthStatus:
    auth_method: str
    storage: str
    keyring_backend: str
    credential_source: str
    scopes: frozenset[str]


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

    def status(self, *, env: Mapping[str, str]) -> GwsAuthStatus:
        stdout = self._run(["auth", "status"], env=env)
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            raise GwsMailAuthStatusError("gws_auth_status_invalid") from None
        return parse_gws_auth_status(payload)

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
        self._auth_status: GwsAuthStatus | None = None

    def auth(self) -> None:
        self._auth_status = None
        self.account.config_dir.mkdir(parents=True, exist_ok=True)
        self.runner.auth(env=self._env())
        invalidate_gws_token_cache(self.account.config_dir)
        self._auth_status = None
        self.verify_identity()

    def status(self) -> bool:
        self.verify_identity()
        return True

    def verify_identity(self) -> IdentityCheck:
        self._require_safe_auth_status()
        response = self.runner.run_read(
            GmailReadRequest(PROFILE_METHOD, {"userId": "me"}), env=self._env()
        )
        if not isinstance(response, dict):
            raise GwsMailIdentityError()
        if response.get("emailAddress") != self.account.email:
            raise GwsMailIdentityError()
        return IdentityCheck(alias=self.account.alias)

    def _require_safe_auth_status(self) -> GwsAuthStatus:
        if self._auth_status is None:
            status = self.runner.status(env=self._env())
            self._auth_status = (
                status
                if isinstance(status, GwsAuthStatus)
                else parse_gws_auth_status(status)
            )
        return self._auth_status

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

    def get_content(self, message_id: str, *, max_bytes: int) -> MessageContent:
        self._require_verified_binding()
        _validate_message_id(message_id)
        validate_content_max_bytes(max_bytes)
        self.verify_identity()
        response = self.runner.run_read(
            GmailReadRequest(
                MESSAGE_GET_METHOD,
                {"userId": "me", "id": message_id, "format": "full"},
            ),
            env=self._env(),
        )
        return _normalize_gmail_content(
            response,
            expected_id=message_id,
            max_bytes=max_bytes,
            fetch_body=lambda attachment_id: self._read_provider_attachment(
                message_id, attachment_id
            ),
        )

    def get_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        max_bytes: int,
    ) -> bytes:
        self._require_verified_binding()
        _validate_message_id(message_id)
        validate_attachment_max_bytes(max_bytes)
        kind, provider_id = _decode_gmail_attachment_id(
            attachment_id, expected_message_id=message_id
        )
        self.verify_identity()
        if kind == "api":
            payload = self._read_provider_attachment(message_id, provider_id)
        else:
            response = self.runner.run_read(
                GmailReadRequest(
                    MESSAGE_GET_METHOD,
                    {"userId": "me", "id": message_id, "format": "full"},
                ),
                env=self._env(),
            )
            payload = _gmail_inline_attachment(
                response,
                expected_id=message_id,
                part_id=provider_id,
            )
        if len(payload) > max_bytes:
            raise MailContentPolicyError(
                "Attachment exceeds the selected max-bytes limit."
            )
        return payload

    def _read_provider_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self.runner.run_read(
            GmailReadRequest(
                ATTACHMENT_GET_METHOD,
                {"userId": "me", "messageId": message_id, "id": attachment_id},
            ),
            env=self._env(),
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), str):
            raise GwsMailError("Gmail attachment response is invalid.")
        return _decode_gmail_data(response["data"])

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

    if request.method == ATTACHMENT_GET_METHOD:
        if set(params) != {"userId", "messageId", "id"} or params.get("userId") != "me":
            raise GwsMailPolicyError(
                "Gmail attachment request parameters are not allowed."
            )
        message_id = params.get("messageId")
        attachment_id = params.get("id")
        if not isinstance(message_id, str) or not isinstance(attachment_id, str):
            raise GwsMailPolicyError("Gmail attachment request is invalid.")
        _validate_message_id(message_id)
        _validate_provider_attachment_id(attachment_id)
        return

    if set(params) == {"userId", "id", "format"}:
        if params.get("userId") != "me" or params.get("format") != "full":
            raise GwsMailPolicyError("Gmail selected content request is invalid.")
        message_id = params.get("id")
        if not isinstance(message_id, str):
            raise GwsMailPolicyError("Gmail message id is invalid.")
        _validate_message_id(message_id)
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


def parse_gws_auth_status(payload: object) -> GwsAuthStatus:
    """Validate the selected profile's GWS status without retaining sensitive fields."""

    if not isinstance(payload, dict):
        raise GwsMailAuthStatusError("gws_auth_status_invalid")

    string_fields = ("auth_method", "storage", "keyring_backend", "credential_source")
    boolean_fields = (
        "client_config_exists",
        "encrypted_credentials_exists",
        "plain_credentials_exists",
    )
    if any(not isinstance(payload.get(name), str) for name in string_fields) or any(
        not isinstance(payload.get(name), bool) for name in boolean_fields
    ):
        raise GwsMailAuthStatusError("gws_auth_status_invalid")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise GwsMailAuthStatusError("gws_auth_status_invalid")
    if "token_env_var" in payload and not isinstance(payload["token_env_var"], bool):
        raise GwsMailAuthStatusError("gws_auth_status_invalid")

    if payload["keyring_backend"] != KEYRING_BACKEND:
        raise GwsMailAuthStatusError("gws_auth_backend_invalid")
    if (
        payload["auth_method"] != "oauth2"
        or payload["storage"] != "encrypted"
        or payload["credential_source"] != "client_secret.json"
        or payload["client_config_exists"] is not True
        or payload["encrypted_credentials_exists"] is not True
        or payload["plain_credentials_exists"] is not False
        or payload.get("token_env_var") is True
    ):
        raise GwsMailAuthStatusError("gws_auth_source_invalid")

    encryption_valid = payload.get("encryption_valid")
    if not isinstance(encryption_valid, bool):
        raise GwsMailAuthStatusError("gws_auth_status_invalid")
    if not encryption_valid:
        raise GwsMailAuthStatusError("gws_auth_decryption_failed")

    has_refresh_token = payload.get("has_refresh_token")
    if not isinstance(has_refresh_token, bool):
        raise GwsMailAuthStatusError("gws_auth_status_invalid")
    if not has_refresh_token:
        raise GwsMailAuthStatusError("gws_auth_token_invalid")

    token_valid = payload.get("token_valid")
    if not isinstance(token_valid, bool):
        raise GwsMailAuthStatusError("gws_auth_token_invalid")
    if not token_valid:
        token_error = payload.get("token_error")
        if isinstance(token_error, str) and _is_invalid_client_error(token_error):
            raise GwsMailAuthStatusError("gws_auth_invalid_client")
        raise GwsMailAuthStatusError("gws_auth_token_invalid")

    raw_scopes = payload.get("scopes")
    if not isinstance(raw_scopes, list) or any(
        not isinstance(scope, str) or not scope for scope in raw_scopes
    ):
        raise GwsMailAuthStatusError("gws_auth_scopes_invalid")
    unique_scopes: set[str] = set()
    for scope in raw_scopes:
        if scope in unique_scopes:
            raise GwsMailAuthStatusError("gws_auth_scopes_invalid")
        unique_scopes.add(scope)
    scopes = frozenset(unique_scopes)
    if scopes not in (
        GWS_REQUIRED_SCOPES,
        GWS_REQUIRED_SCOPES | GWS_ECHOED_OIDC_SCOPE_ALIASES,
    ):
        raise GwsMailAuthStatusError("gws_auth_scopes_invalid")

    return GwsAuthStatus(
        auth_method=payload["auth_method"],
        storage=payload["storage"],
        keyring_backend=payload["keyring_backend"],
        credential_source=payload["credential_source"],
        scopes=GWS_REQUIRED_SCOPES,
    )


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
    account_home, account_name = _canonical_os_account()
    selected_dir = config_dir.expanduser().resolve()
    clean = {
        name: value
        for name in SAFE_SUBPROCESS_ENV_VARS
        if (value := os.environ.get(name)) is not None
    }
    clean["HOME"] = str(account_home)
    clean["LOGNAME"] = account_name
    clean["USER"] = account_name
    clean["GOOGLE_APPLICATION_CREDENTIALS"] = str(
        _adc_denial_sentinel(selected_dir)
    )
    clean["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(selected_dir)
    clean["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = KEYRING_BACKEND
    return clean


def invalidate_gws_token_cache(config_dir: Path) -> None:
    """Remove derived access-token state after a successful OAuth login."""

    try:
        (config_dir / TOKEN_CACHE_FILENAME).unlink(missing_ok=True)
    except OSError as exc:
        raise GwsMailError("gws token cache could not be invalidated.") from exc


def reject_ambient_credential_overrides() -> None:
    if any(name in os.environ for name in FORBIDDEN_CREDENTIAL_ENV_VARS):
        raise GwsMailPolicyError("Ambient Google credential override is forbidden.")


def validate_selected_environment(env: Mapping[str, str]) -> dict[str, str]:
    reject_ambient_credential_overrides()
    allowed = SAFE_SUBPROCESS_ENV_VARS | SELECTED_GWS_ENV_VARS
    if set(env) - allowed:
        raise GwsMailPolicyError("Unexpected environment variable for gws.")
    selected_dir = env.get("GOOGLE_WORKSPACE_CLI_CONFIG_DIR")
    if not isinstance(selected_dir, str) or not selected_dir:
        raise GwsMailPolicyError("Selected gws config directory is missing.")
    selected_path = Path(selected_dir)
    if (
        not selected_path.is_absolute()
        or str(selected_path.resolve()) != selected_dir
    ):
        raise GwsMailPolicyError("Selected gws config directory is invalid.")
    account_home, account_name = _canonical_os_account()
    if (
        env.get("HOME") != str(account_home)
        or env.get("USER") != account_name
        or env.get("LOGNAME") != account_name
    ):
        raise GwsMailPolicyError(OS_ACCOUNT_ENV_ERROR_MESSAGE)
    expected_sentinel = str(_adc_denial_sentinel(selected_path))
    if env.get("GOOGLE_APPLICATION_CREDENTIALS") != expected_sentinel:
        raise GwsMailPolicyError("Selected gws ADC denial sentinel is invalid.")
    _reject_fallback_credential_artifacts(selected_path)
    if env.get("GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND") != KEYRING_BACKEND:
        raise GwsMailPolicyError("gws keyring backend is not enforced.")
    return dict(env)


def _reject_fallback_credential_artifacts(selected_dir: Path) -> None:
    candidates = (
        selected_dir / "credentials.json",
        _adc_denial_sentinel(selected_dir),
    )
    for candidate in candidates:
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise GwsMailPolicyError(FALLBACK_CREDENTIAL_ERROR_MESSAGE) from None
        raise GwsMailPolicyError(FALLBACK_CREDENTIAL_ERROR_MESSAGE)


def _adc_denial_sentinel(selected_dir: Path) -> Path:
    return selected_dir / ADC_DENIAL_SENTINEL_FILENAME


def _canonical_os_account() -> tuple[Path, str]:
    try:
        record = pwd.getpwuid(os.getuid())
        home_value = record.pw_dir
        account_name = record.pw_name
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        raise GwsMailPolicyError(OS_ACCOUNT_ERROR_MESSAGE) from None
    if (
        not isinstance(home_value, str)
        or not home_value
        or not isinstance(account_name, str)
        or not account_name
        or account_name.strip() != account_name
        or any(character.isspace() for character in account_name)
        or "/" in account_name
        or ":" in account_name
        or "\x00" in account_name
    ):
        raise GwsMailPolicyError(OS_ACCOUNT_ERROR_MESSAGE)
    account_home = Path(home_value)
    if not account_home.is_absolute():
        raise GwsMailPolicyError(OS_ACCOUNT_ERROR_MESSAGE)
    try:
        account_home = account_home.resolve(strict=True)
        if not account_home.is_dir():
            raise GwsMailPolicyError(OS_ACCOUNT_ERROR_MESSAGE)
    except (OSError, RuntimeError):
        raise GwsMailPolicyError(OS_ACCOUNT_ERROR_MESSAGE) from None

    ambient_home = os.environ.get("HOME")
    if ambient_home is not None:
        ambient_path = Path(ambient_home)
        try:
            if (
                not ambient_path.is_absolute()
                or ambient_path.resolve(strict=True) != account_home
            ):
                raise GwsMailPolicyError(OS_ACCOUNT_ENV_ERROR_MESSAGE)
        except (OSError, RuntimeError):
            raise GwsMailPolicyError(OS_ACCOUNT_ENV_ERROR_MESSAGE) from None
    if any(
        (value := os.environ.get(name)) is not None and value != account_name
        for name in ("USER", "LOGNAME")
    ):
        raise GwsMailPolicyError(OS_ACCOUNT_ENV_ERROR_MESSAGE)
    return account_home, account_name


def _query_uses_forbidden_selector(query: str) -> bool:
    return any(
        re.search(pattern, query, flags=re.IGNORECASE)
        for pattern in FORBIDDEN_QUERY_SELECTOR_PATTERNS
    )


def _is_invalid_client_error(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return (
        "invalid_client" in normalized
        or "oauth client was not found" in normalized
    )


def _method_segments(method: str) -> tuple[str, ...]:
    mapping = {
        PROFILE_METHOD: ("users", "getProfile"),
        MESSAGE_LIST_METHOD: ("users", "messages", "list"),
        MESSAGE_GET_METHOD: ("users", "messages", "get"),
        ATTACHMENT_GET_METHOD: ("users", "messages", "attachments", "get"),
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


def _normalize_gmail_content(
    response: object,
    *,
    expected_id: str,
    max_bytes: int,
    fetch_body,
) -> MessageContent:
    metadata = sanitize_message_metadata(response, expected_id=expected_id)
    if not isinstance(response, dict) or not isinstance(response.get("payload"), dict):
        raise GwsMailError("Gmail content response is invalid.")
    text_chunks: list[str] = []
    html_chunks: list[str] = []
    text_truncated = False
    html_truncated = False
    attachments: list[AttachmentMetadata] = []

    def visit(part: object, path: tuple[int, ...]) -> None:
        nonlocal text_truncated, html_truncated
        if not isinstance(part, dict):
            raise GwsMailError("Gmail content response is invalid.")
        mime_type = sanitize_content_type(str(part.get("mimeType", "")))
        filename = (
            sanitize_filename(str(part.get("filename", "")))
            if part.get("filename")
            else ""
        )
        body = part.get("body", {})
        if not isinstance(body, dict):
            raise GwsMailError("Gmail content response is invalid.")
        part_id = str(part.get("partId", ".".join(str(item) for item in path)))
        provider_attachment_id = body.get("attachmentId")
        inline_data = body.get("data")
        declared_size = body.get("size", 0)
        size = (
            declared_size
            if isinstance(declared_size, int) and declared_size >= 0
            else 0
        )
        children = part.get("parts", [])
        if children:
            if not isinstance(children, list):
                raise GwsMailError("Gmail content response is invalid.")
            for index, child in enumerate(children, start=1):
                visit(child, (*path, index))
            return

        is_attachment = bool(filename) or (
            mime_type not in {"text/plain", "text/html"}
            and (
                isinstance(provider_attachment_id, str) or isinstance(inline_data, str)
            )
        )
        if is_attachment:
            if isinstance(provider_attachment_id, str):
                _validate_provider_attachment_id(provider_attachment_id)
                token = f"api:{provider_attachment_id}"
            elif isinstance(inline_data, str):
                token = f"part:{part_id}"
            else:
                raise GwsMailError("Gmail attachment response is invalid.")
            attachments.append(
                AttachmentMetadata(
                    id=_encode_gmail_attachment_id(expected_id, token),
                    filename=filename or "attachment",
                    content_type=mime_type,
                    size=size,
                )
            )
            return
        if mime_type not in {"text/plain", "text/html"}:
            return
        if isinstance(inline_data, str):
            payload = _decode_gmail_data(inline_data)
        elif isinstance(provider_attachment_id, str):
            _validate_provider_attachment_id(provider_attachment_id)
            payload = fetch_body(provider_attachment_id)
        else:
            payload = b""
        value, truncated = decode_text(
            payload,
            charset=_gmail_part_charset(part),
            max_bytes=max_bytes,
        )
        if mime_type == "text/plain":
            text_chunks.append(value)
            text_truncated |= truncated
        else:
            html_chunks.append(value)
            html_truncated |= truncated

    visit(response["payload"], ())
    text, joined_text_truncated = _join_gmail_text(text_chunks, max_bytes=max_bytes)
    html_text, joined_html_truncated = _join_gmail_text(
        html_chunks, max_bytes=max_bytes
    )
    text_truncated |= joined_text_truncated
    html_truncated |= joined_html_truncated
    safe_html = sanitize_html(html_text) if html_text else ""
    if not text and safe_html:
        text = html_to_text(safe_html)
    return MessageContent(
        id=metadata.id,
        sender=metadata.sender,
        to=metadata.to,
        subject=metadata.subject,
        date=metadata.date,
        text=text,
        html=safe_html,
        text_truncated=text_truncated,
        html_truncated=html_truncated,
        attachments=tuple(attachments),
    )


def _decode_gmail_data(value: str) -> bytes:
    if len(value) > 140_000_000:
        raise MailContentPolicyError("Gmail encoded content is too large.")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise GwsMailError("Gmail encoded content is invalid.") from None


def _encode_gmail_attachment_id(message_id: str, token: str) -> str:
    payload = f"{message_id}\0{token}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return GMAIL_ATTACHMENT_ID_PREFIX + encoded


def _decode_gmail_attachment_id(
    value: str, *, expected_message_id: str
) -> tuple[str, str]:
    if not isinstance(value, str) or not value.startswith(GMAIL_ATTACHMENT_ID_PREFIX):
        raise GwsMailPolicyError("Gmail attachment id is invalid.")
    token = value.removeprefix(GMAIL_ATTACHMENT_ID_PREFIX)
    try:
        raw = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        ).decode("utf-8")
        message_id, kind_and_id = raw.split("\0", 1)
        kind, provider_id = kind_and_id.split(":", 1)
    except (binascii.Error, UnicodeError, ValueError):
        raise GwsMailPolicyError("Gmail attachment id is invalid.") from None
    if (
        message_id != expected_message_id
        or kind not in {"api", "part"}
        or not provider_id
    ):
        raise GwsMailPolicyError("Gmail attachment id is invalid.")
    if kind == "api":
        _validate_provider_attachment_id(provider_id)
    elif re.fullmatch(r"[A-Za-z0-9_.-]+", provider_id) is None:
        raise GwsMailPolicyError("Gmail attachment id is invalid.")
    return kind, provider_id


def _validate_provider_attachment_id(value: str) -> None:
    if not value or len(value) > 2048 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise GwsMailPolicyError("Gmail attachment id is invalid.")


def _gmail_inline_attachment(
    response: object, *, expected_id: str, part_id: str
) -> bytes:
    if not isinstance(response, dict) or response.get("id") != expected_id:
        raise GwsMailError("Gmail content response is invalid.")
    root = response.get("payload")
    found: list[bytes] = []

    def visit(part: object) -> None:
        if not isinstance(part, dict):
            return
        if str(part.get("partId", "")) == part_id:
            body = part.get("body")
            if isinstance(body, dict) and isinstance(body.get("data"), str):
                found.append(_decode_gmail_data(body["data"]))
        children = part.get("parts", [])
        if isinstance(children, list):
            for child in children:
                visit(child)

    visit(root)
    if len(found) != 1:
        raise GwsMailError("Gmail attachment response is invalid.")
    return found[0]


def _gmail_part_charset(part: Mapping[str, object]) -> str | None:
    headers = part.get("headers", [])
    if not isinstance(headers, list):
        return None
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if (
            isinstance(name, str)
            and name.casefold() == "content-type"
            and isinstance(value, str)
        ):
            match = re.search(
                r'\bcharset\s*=\s*(?:"([^"]+)"|([^;\s]+))',
                value,
                flags=re.IGNORECASE,
            )
            if match:
                return match.group(1) or match.group(2)
    return None


def _join_gmail_text(values: list[str], *, max_bytes: int) -> tuple[str, bool]:
    raw = "\n\n".join(values).encode("utf-8")
    return (
        raw[:max_bytes].decode("utf-8", errors="ignore"),
        len(raw) > max_bytes,
    )
