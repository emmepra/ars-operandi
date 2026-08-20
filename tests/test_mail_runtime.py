from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ars_operandi_mail.config import (
    GwsAccountConfig,
    MailConfigError,
    ProtonBridgeAccountConfig,
    load_mail_config,
)
from ars_operandi_mail.gws import (
    ATTACHMENT_GET_METHOD,
    GmailReadRequest,
    GwsAuthStatus,
    GwsMailAuthStatusError,
    GwsMailClient,
    GwsMailCommandRunner,
    GwsMailError,
    GwsMailIdentityError,
    GwsMailPolicyError,
    MESSAGE_GET_METHOD,
    MESSAGE_LIST_METHOD,
    MessageMetadata,
    OnboardingProbe,
    build_query,
    clean_gws_environment,
    parse_gws_auth_status,
    validate_gmail_request,
    validate_selected_environment,
)
from ars_operandi_mail.mail_content import AttachmentMetadata, MessageContent
from ars_operandi_mail.proton import (
    BODYSTRUCTURE_FETCH_ITEM,
    HEADER_FETCH_ITEM,
    KEYCHAIN_SERVICE_PREFIX,
    MacOSKeychainSecretResolver,
    MemoryCachingSecretResolver,
    ProtonBridgeConfigError,
    ProtonBridgeIdentityError,
    ProtonBridgeMailClient,
    ProtonBridgeMailError,
    ProtonBridgePolicyError,
    _build_search_criteria,
    _encode_message_id,
    _metadata_from_headers,
    _validate_max_results,
)
from ars_operandi_mail.service import (
    MailRuntime,
    build_mcp,
    safe_error_payload,
    safe_tool_call,
)


def write_index(
    root: Path, *, state: str = "verified", adapter: str = "proton"
) -> Path:
    path = root / "projects.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "mail_connections": {
                    "mail-home": {
                        "adapter": "gws",
                        "account_alias": "home",
                        "expected_email": "home@example.test",
                        "execution_host": "local-mac",
                        "access_mode": "read-only",
                        "binding_state": state,
                    },
                    "mail-work": {
                        "adapter": adapter,
                        "account_alias": "work",
                        "expected_email": "work@example.test",
                        "execution_host": "local-mac",
                        "access_mode": "read-only",
                        "binding_state": state,
                    },
                },
                "mail_profiles": {
                    "home": {"connections": ["mail-home"]},
                    "work": {"connections": ["mail-work"]},
                },
                "projects": [
                    {"key": "personal/example", "mail_profile": "home"},
                    {"key": "work/example", "mail_profile": "work"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def write_proton_config(
    root: Path,
    *,
    alias: str = "work",
    email: str = "work@example.test",
    certificate: bytes = b"certificate",
) -> Path:
    path = root / "proton" / f"{alias}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                'host = "127.0.0.1"',
                "port = 1143",
                'transport = "starttls"',
                f'tls_cert_sha256 = "{hashlib.sha256(certificate).hexdigest()}"',
                'identity_mode = "dedicated-account"',
                f'bridge_username = "{email}"',
                f'keychain_service = "{KEYCHAIN_SERVICE_PREFIX}{alias}"',
                f'keychain_account = "{email}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


def valid_gws_auth_status(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "auth_method": "oauth2",
        "storage": "encrypted",
        "keyring_backend": "keyring",
        "client_config_exists": True,
        "encrypted_credentials_exists": True,
        "plain_credentials_exists": False,
        "credential_source": "client_secret.json",
        "project_id": "example-project",
        "encryption_valid": True,
        "has_refresh_token": True,
        "token_valid": True,
        "scopes": [
            "openid",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ],
    }
    payload.update(overrides)
    return payload


class FakeSecretBackend:
    def __init__(self, events: list[object] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.events = events

    def resolve(self, *, service: str, account: str, timeout_seconds: float) -> str:
        self.calls.append((service, account))
        if self.events is not None:
            self.events.append("secret")
        return "bridge-secret"


class FakeSock:
    def __init__(self, events: list[object], certificate: bytes) -> None:
        self.events = events
        self.certificate = certificate

    def settimeout(self, value: float) -> None:
        self.events.append(("timeout", value))

    def getpeercert(self, *, binary_form: bool) -> bytes:
        assert binary_form
        self.events.append("certificate")
        return self.certificate


class FakeImap:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        events: list[object],
        certificate: bytes,
    ) -> None:
        self.events = events
        self.sock = FakeSock(events, certificate)
        self.debug = 10
        events.append(("connect", host, port, timeout))

    def capability(self):
        self.events.append("capability")
        return "OK", [b"IMAP4rev1 STARTTLS"]

    def starttls(self, context):
        self.events.append("starttls")
        return "OK", [b"ready"]

    def login(self, username: str, secret: str):
        self.events.append(("login", username, secret))
        return "OK", [b"logged"]

    def select(self, mailbox: str, *, readonly: bool):
        self.events.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def response(self, name: str):
        self.events.append(("response", name))
        return "UIDVALIDITY", [b"7"]

    def uid(self, command: str, *args):
        self.events.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [b"1"]
        literal = b"From: Sender <sender@example.test>\r\nTo: work@example.test\r\nSubject: bounded\r\nDate: Mon, 20 Jul 2026 10:00:00 +0200\r\n\r\n"
        return "OK", [
            (b"1 (UID 1 BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {120}", literal),
            b")",
        ]

    def logout(self):
        self.events.append("logout")
        return "OK", [b"bye"]


class ConfigTests(unittest.TestCase):
    def test_manifest_is_consumer_neutral_and_routes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_mail_config(write_index(root), config_root=root / "local")

        self.assertEqual(set(config.accounts), {"home", "work"})
        self.assertEqual(config.get_project_account("work/example").provider, "proton")
        self.assertEqual(config.get_account("home").provider, "gws")

    def test_unknown_provider_fails_before_client_or_secret_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(MailConfigError, "provider"):
                load_mail_config(
                    write_index(root, adapter="imap"), config_root=root / "local"
                )


class GwsPolicyTests(unittest.TestCase):
    def test_auth_invalidates_token_cache_before_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            token_cache = config_dir / "token_cache.json"
            token_cache.write_bytes(b"stale encrypted token")
            events: list[str] = []

            class Runner:
                def auth(self, *, env):
                    events.append("auth")

                def status(self, *, env):
                    if token_cache.exists():
                        raise AssertionError("stale token cache reached status check")
                    events.append("status")
                    return valid_gws_auth_status()

                def run_read(self, request, *, env):
                    if token_cache.exists():
                        raise AssertionError("stale token cache reached identity check")
                    events.append(request.method)
                    return {"emailAddress": "home@example.test"}

            client = GwsMailClient(
                GwsAccountConfig(
                    alias="home",
                    email="home@example.test",
                    config_dir=config_dir,
                    binding_state="verified",
                ),
                runner=Runner(),
            )

            client.auth()

            self.assertEqual(events, ["auth", "status", "users.getProfile"])
            self.assertFalse(token_cache.exists())

    def test_auth_with_missing_token_cache_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)

            class Runner:
                def auth(self, *, env):
                    return None

                def status(self, *, env):
                    return valid_gws_auth_status()

                def run_read(self, request, *, env):
                    return {"emailAddress": "home@example.test"}

            client = GwsMailClient(
                GwsAccountConfig(
                    alias="home",
                    email="home@example.test",
                    config_dir=config_dir,
                    binding_state="verified",
                ),
                runner=Runner(),
            )

            client.auth()

    def test_failed_auth_preserves_existing_token_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            token_cache = config_dir / "token_cache.json"
            token_cache.write_bytes(b"existing encrypted token")

            class Runner:
                def auth(self, *, env):
                    raise GwsMailError("gws operation failed (exit code 2).")

                def run_read(self, request, *, env):
                    raise AssertionError("identity check must not run after failed auth")

            client = GwsMailClient(
                GwsAccountConfig(
                    alias="home",
                    email="home@example.test",
                    config_dir=config_dir,
                    binding_state="verified",
                ),
                runner=Runner(),
            )

            with self.assertRaises(GwsMailError):
                client.auth()

            self.assertEqual(token_cache.read_bytes(), b"existing encrypted token")

    def test_cache_invalidation_failure_stops_before_identity_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "token_cache.json").write_bytes(b"stale encrypted token")

            class Runner:
                def auth(self, *, env):
                    return None

                def status(self, *, env):
                    raise AssertionError("status must not run with stale cache")

                def run_read(self, request, *, env):
                    raise AssertionError("identity check must not use stale cache")

            client = GwsMailClient(
                GwsAccountConfig(
                    alias="home",
                    email="home@example.test",
                    config_dir=config_dir,
                    binding_state="verified",
                ),
                runner=Runner(),
            )

            with patch.object(Path, "unlink", side_effect=PermissionError):
                with self.assertRaisesRegex(GwsMailError, "token cache"):
                    client.auth()

    def test_command_runner_parses_status_json_without_retaining_sensitive_fields(
        self,
    ) -> None:
        runner = GwsMailCommandRunner()
        provider_payload = valid_gws_auth_status(
            client_config="/private/sentinel/client.json",
            config_client_id="sensitive-client-id",
            project_id="sensitive-project-id",
            token_env_var=False,
            user="sensitive@example.test",
        )
        with patch.object(runner, "_run", return_value=json.dumps(provider_payload)):
            status = runner.status(env={})

        rendered = repr(status)
        self.assertEqual(
            status.scopes,
            {
                "openid",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            },
        )
        self.assertNotIn("/private/sentinel", rendered)
        self.assertNotIn("sensitive-client-id", rendered)
        self.assertNotIn("sensitive-project-id", rendered)
        self.assertNotIn("sensitive@example.test", rendered)

    def test_command_runner_rejects_malformed_status_without_raw_output(self) -> None:
        runner = GwsMailCommandRunner()
        raw = '{"secret":"raw-provider-detail"'
        with patch.object(runner, "_run", return_value=raw):
            with self.assertRaises(GwsMailAuthStatusError) as caught:
                runner.status(env={})

        self.assertEqual(caught.exception.code, "gws_auth_status_invalid")
        self.assertEqual(
            str(caught.exception),
            "The selected GWS authentication status is invalid.",
        )
        self.assertNotIn("raw-provider-detail", str(caught.exception))

    def test_invalid_status_has_typed_sanitized_errors_and_never_reads_provider(
        self,
    ) -> None:
        cases = (
            (
                "schema",
                {"auth_method": "oauth2"},
                "gws_auth_status_invalid",
                "The selected GWS authentication status is invalid.",
            ),
            (
                "empty-project-id",
                valid_gws_auth_status(project_id=""),
                "gws_auth_status_invalid",
                "The selected GWS authentication status is invalid.",
            ),
            (
                "invalid-project-id-type",
                valid_gws_auth_status(project_id=123),
                "gws_auth_status_invalid",
                "The selected GWS authentication status is invalid.",
            ),
            (
                "invalid-token-env-flag-type",
                valid_gws_auth_status(token_env_var="false"),
                "gws_auth_status_invalid",
                "The selected GWS authentication status is invalid.",
            ),
            (
                "ambient-token-source",
                valid_gws_auth_status(token_env_var=True),
                "gws_auth_source_invalid",
                "The selected GWS profile is not using the required encrypted OAuth source.",
            ),
            (
                "backend",
                valid_gws_auth_status(keyring_backend="file"),
                "gws_auth_backend_invalid",
                "The selected GWS profile is not using the required keyring backend.",
            ),
            (
                "source",
                valid_gws_auth_status(
                    storage="plaintext",
                    encrypted_credentials_exists=False,
                    plain_credentials_exists=True,
                    credential_source="environment_variables",
                ),
                "gws_auth_source_invalid",
                "The selected GWS profile is not using the required encrypted OAuth source.",
            ),
            (
                "decryption",
                valid_gws_auth_status(encryption_valid=False),
                "gws_auth_decryption_failed",
                "The selected GWS encrypted credentials cannot be decrypted.",
            ),
            (
                "invalid-client",
                valid_gws_auth_status(
                    token_valid=False,
                    token_error=(
                        "invalid_client raw-provider-detail sensitive@example.test "
                        "/private/sentinel/client.json"
                    ),
                ),
                "gws_auth_invalid_client",
                "The selected GWS OAuth client is invalid.",
            ),
            (
                "token",
                valid_gws_auth_status(
                    token_valid=False,
                    token_error="provider token detail raw-provider-detail",
                ),
                "gws_auth_token_invalid",
                "The selected GWS OAuth token is unavailable or invalid.",
            ),
            (
                "refresh-token",
                valid_gws_auth_status(has_refresh_token=False),
                "gws_auth_token_invalid",
                "The selected GWS OAuth token is unavailable or invalid.",
            ),
            (
                "scopes",
                valid_gws_auth_status(
                    scopes=[
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.modify",
                    ]
                ),
                "gws_auth_scopes_invalid",
                "The selected GWS OAuth scopes do not match the required Gmail read-only set.",
            ),
            (
                "missing-provider-identity-scope",
                valid_gws_auth_status(
                    scopes=[
                        "openid",
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/userinfo.email",
                    ]
                ),
                "gws_auth_scopes_invalid",
                "The selected GWS OAuth scopes do not match the required Gmail read-only set.",
            ),
            (
                "extra-service-scope",
                valid_gws_auth_status(
                    scopes=[
                        "openid",
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/userinfo.email",
                        "https://www.googleapis.com/auth/userinfo.profile",
                        "https://www.googleapis.com/auth/drive.readonly",
                    ]
                ),
                "gws_auth_scopes_invalid",
                "The selected GWS OAuth scopes do not match the required Gmail read-only set.",
            ),
        )
        runtime = MailRuntime(
            project_index=Path("/tmp/nonexistent-project-index"),
            config_root=Path("/tmp/nonexistent-mail-config"),
        )

        for name, status_payload, code, message in cases:
            with self.subTest(name=name):
                reads: list[str] = []

                class Runner:
                    def status(self, *, env):
                        return status_payload

                    def run_read(self, request, *, env):
                        reads.append(request.method)
                        raise AssertionError("provider read must not start")

                client = GwsMailClient(
                    GwsAccountConfig(
                        alias="home",
                        email="home@example.test",
                        config_dir=Path("/tmp/home"),
                        binding_state="verified",
                    ),
                    runner=Runner(),
                )

                with self.assertRaises(GwsMailAuthStatusError) as caught:
                    client.status()
                result = safe_error_payload(runtime, caught.exception)

                self.assertEqual(reads, [])
                self.assertEqual(result, {"code": code, "message": message})
                rendered = repr(result)
                self.assertNotIn("raw-provider-detail", rendered)
                self.assertNotIn("sensitive@example.test", rendered)
                self.assertNotIn("/private/sentinel", rendered)

    def test_valid_status_precedes_exact_identity_provider_read(self) -> None:
        events: list[str] = []

        class Runner:
            def status(self, *, env):
                events.append("auth.status")
                return valid_gws_auth_status()

            def run_read(self, request, *, env):
                events.append(request.method)
                return {"emailAddress": "home@example.test"}

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )

        self.assertTrue(client.status())
        self.assertEqual(events, ["auth.status", "users.getProfile"])

    def test_parser_rejects_safe_dataclass_but_client_accepts_runner_result(self) -> None:
        status = GwsAuthStatus(
            auth_method="oauth2",
            storage="encrypted",
            keyring_backend="keyring",
            credential_source="client_secret.json",
            scopes=frozenset(
                {
                    "openid",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                }
            ),
        )
        with self.assertRaises(GwsMailAuthStatusError) as caught:
            parse_gws_auth_status(status)
        self.assertEqual(caught.exception.code, "gws_auth_status_invalid")

        class Runner:
            def status(self, *, env):
                return status

            def run_read(self, request, *, env):
                return {"emailAddress": "home@example.test"}

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )
        self.assertTrue(client.status())

    def test_multi_message_search_preflights_status_once_per_client(self) -> None:
        events: list[str] = []

        class Runner:
            def status(self, *, env):
                events.append("auth.status")
                return valid_gws_auth_status()

            def run_read(self, request, *, env):
                events.append(request.method)
                if request.method == "users.getProfile":
                    return {"emailAddress": "home@example.test"}
                if request.method == MESSAGE_LIST_METHOD:
                    return {"messages": [{"id": "msg_1"}, {"id": "msg_2"}]}
                if request.method == MESSAGE_GET_METHOD:
                    return {
                        "id": request.params["id"],
                        "payload": {"headers": []},
                    }
                raise AssertionError(request.method)

        account = GwsAccountConfig(
            alias="home",
            email="home@example.test",
            config_dir=Path("/tmp/home"),
            binding_state="verified",
        )
        client = GwsMailClient(account, runner=Runner())

        messages = client.search(
            query="",
            after="2026-07-01",
            before="2026-07-02",
            max_results=2,
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(events.count("auth.status"), 1)

        second_client = GwsMailClient(account, runner=Runner())
        self.assertTrue(second_client.status())
        self.assertEqual(events.count("auth.status"), 2)

    def test_auth_rechecks_status_after_cached_preflight(self) -> None:
        events: list[str] = []

        class Runner:
            def auth(self, *, env):
                events.append("auth")

            def status(self, *, env):
                events.append("auth.status")
                return valid_gws_auth_status()

            def run_read(self, request, *, env):
                events.append(request.method)
                return {"emailAddress": "home@example.test"}

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )
        self.assertTrue(client.status())
        events.clear()

        client.auth()

        self.assertEqual(events, ["auth", "auth.status", "users.getProfile"])

    def test_invalid_status_blocks_normal_mailbox_read_before_provider(self) -> None:
        reads: list[str] = []

        class Runner:
            def status(self, *, env):
                return valid_gws_auth_status(encryption_valid=False)

            def run_read(self, request, *, env):
                reads.append(request.method)
                raise AssertionError("provider read must not start")

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )

        with self.assertRaises(GwsMailAuthStatusError):
            client.get_metadata("msg_1")
        self.assertEqual(reads, [])

    def test_selected_content_is_normalized_and_attachment_is_explicit(self) -> None:
        calls: list[str] = []

        class Runner:
            def status(self, *, env):
                calls.append("auth.status")
                return valid_gws_auth_status()

            def run_read(self, request, *, env):
                calls.append(request.method)
                if request.method == "users.getProfile":
                    return {"emailAddress": "home@example.test"}
                if request.method == MESSAGE_GET_METHOD:
                    return {
                        "id": "msg_1",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "sender@example.test"},
                                {"name": "To", "value": "home@example.test"},
                                {"name": "Subject", "value": "Selected"},
                                {"name": "Date", "value": "Thu, 23 Jul 2026"},
                            ],
                            "mimeType": "multipart/mixed",
                            "parts": [
                                {
                                    "partId": "0",
                                    "mimeType": "text/plain",
                                    "filename": "",
                                    "headers": [
                                        {
                                            "name": "Content-Type",
                                            "value": "text/plain; charset=utf-8",
                                        }
                                    ],
                                    "body": {"data": "SGVsbG8gZnJvbSBBcnM"},
                                },
                                {
                                    "partId": "1",
                                    "mimeType": "application/pdf",
                                    "filename": "report.pdf",
                                    "headers": [],
                                    "body": {
                                        "attachmentId": "provider-att",
                                        "size": 3,
                                    },
                                },
                            ],
                        },
                    }
                if request.method == ATTACHMENT_GET_METHOD:
                    return {"data": "UERG"}
                raise AssertionError(request.method)

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )
        content = client.get_content("msg_1", max_bytes=1024).to_dict()
        self.assertEqual(content["content"]["text"], "Hello from Ars")
        attachment_id = content["attachments"][0]["id"]
        self.assertNotIn("provider-att", attachment_id)
        self.assertEqual(
            client.get_attachment("msg_1", attachment_id, max_bytes=10), b"PDF"
        )
        self.assertEqual(
            calls,
            [
                "auth.status",
                "users.getProfile",
                MESSAGE_GET_METHOD,
                "users.getProfile",
                ATTACHMENT_GET_METHOD,
            ],
        )

    def test_query_is_explicitly_bounded_and_escape_operators_are_denied(self) -> None:
        query = build_query(
            query="from:sender@example.test",
            after="2026-07-01",
            before="2026-07-20",
        )
        self.assertIn("after:2026/07/01", query)
        self.assertIn("before:2026/07/20", query)
        for selector in ("x OR y", "{x}", "x|y", "in:anywhere", "newer_than:1d"):
            with self.subTest(selector=selector):
                with self.assertRaises(GwsMailPolicyError):
                    build_query(
                        query=selector,
                        after="2026-07-01",
                        before="2026-07-20",
                    )

    def test_only_fixed_metadata_headers_are_allowed(self) -> None:
        valid = GmailReadRequest(
            MESSAGE_GET_METHOD,
            {
                "userId": "me",
                "id": "abc_1",
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date"],
            },
        )
        validate_gmail_request(valid)
        with self.assertRaises(GwsMailPolicyError):
            validate_gmail_request(
                GmailReadRequest(
                    MESSAGE_GET_METHOD,
                    {**valid.params, "metadataHeaders": ["From", "Received"]},
                )
            )

    def test_ambient_google_credentials_are_rejected(self) -> None:
        for name in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_WORKSPACE_CLI_TOKEN",
            "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
            "GOOGLE_WORKSPACE_CLI_CLIENT_ID",
            "GOOGLE_WORKSPACE_CLI_CLIENT_SECRET",
            "GOOGLE_WORKSPACE_CLI_ACCOUNT",
        ):
            with self.subTest(name=name):
                with patch.dict(os.environ, {name: "sensitive-value"}):
                    with self.assertRaises(GwsMailPolicyError):
                        clean_gws_environment(Path("/tmp/gws"))

    def test_gws_environment_isolates_home_to_absolute_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "selected-profile"
            with patch.dict(os.environ, {"HOME": "/global/home"}):
                env = clean_gws_environment(profile)

        selected = str(profile.resolve())
        self.assertEqual(env["HOME"], selected)
        self.assertEqual(env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"], selected)
        self.assertEqual(validate_selected_environment(env), env)
        adc_path = Path(env["HOME"]) / ".config/gcloud/application_default_credentials.json"
        self.assertEqual(adc_path.parts[: len(profile.resolve().parts)], profile.resolve().parts)
        self.assertNotIn("/global/home", str(adc_path))

    def test_selected_environment_rejects_home_or_path_isolation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selected = str((Path(tmp) / "selected-profile").resolve())
            valid = {
                "HOME": selected,
                "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": selected,
                "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "keyring",
            }
            validate_selected_environment(valid)
            invalid = (
                {**valid, "HOME": str(Path(tmp) / "different-home")},
                {
                    **valid,
                    "HOME": "relative-profile",
                    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": "relative-profile",
                },
                {
                    **valid,
                    "HOME": f"{selected}/../selected-profile",
                    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": (
                        f"{selected}/../selected-profile"
                    ),
                },
            )
            for env in invalid:
                with self.subTest(env=env):
                    with self.assertRaises(GwsMailPolicyError):
                        validate_selected_environment(env)

    def test_preexec_gate_rejects_plaintext_and_isolated_home_adc(self) -> None:
        forbidden = (
            Path("credentials.json"),
            Path(".config/gcloud/application_default_credentials.json"),
        )
        for relative_path in forbidden:
            with self.subTest(relative_path=relative_path):
                with tempfile.TemporaryDirectory() as tmp:
                    profile = Path(tmp) / "selected-profile"
                    profile.mkdir()
                    env = clean_gws_environment(profile)
                    self.assertEqual(validate_selected_environment(env), env)
                    candidate = profile / relative_path
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    candidate.touch()
                    runner = GwsMailCommandRunner()
                    actions = (
                        lambda: runner.status(env=env),
                        lambda: runner.run_read(
                            GmailReadRequest("users.getProfile", {"userId": "me"}),
                            env=env,
                        ),
                        lambda: runner.auth(env=env),
                    )
                    with patch("ars_operandi_mail.gws.subprocess.run") as launch:
                        for action in actions:
                            with self.assertRaises(GwsMailPolicyError) as caught:
                                action()
                            self.assertEqual(
                                str(caught.exception),
                                "GWS plaintext or ADC fallback credentials are forbidden.",
                            )
                            self.assertNotIn(str(profile), str(caught.exception))
                    launch.assert_not_called()

    def test_cached_status_cannot_bypass_preexec_fallback_gate(self) -> None:
        safe_status = GwsAuthStatus(
            auth_method="oauth2",
            storage="encrypted",
            keyring_backend="keyring",
            credential_source="client_secret.json",
            scopes=frozenset(
                {
                    "openid",
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                }
            ),
        )

        class PrimedRunner(GwsMailCommandRunner):
            def __init__(self) -> None:
                super().__init__()
                self.status_calls = 0

            def status(self, *, env):
                self.status_calls += 1
                return safe_status

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "selected-profile"
            profile.mkdir()
            runner = PrimedRunner()
            client = GwsMailClient(
                GwsAccountConfig(
                    alias="home",
                    email="home@example.test",
                    config_dir=profile,
                    binding_state="verified",
                ),
                runner=runner,
            )
            client._require_safe_auth_status()
            (profile / "credentials.json").touch()

            with patch("ars_operandi_mail.gws.subprocess.run") as launch:
                with self.assertRaises(GwsMailPolicyError) as caught:
                    client.verify_identity()

        self.assertEqual(runner.status_calls, 1)
        launch.assert_not_called()
        self.assertEqual(
            str(caught.exception),
            "GWS plaintext or ADC fallback credentials are forbidden.",
        )

    def test_exact_gws_identity_mismatch_fails_closed(self) -> None:
        class Runner:
            def status(self, *, env):
                return valid_gws_auth_status()

            def run_read(self, request, *, env):
                return {"emailAddress": "other@example.test"}

        client = GwsMailClient(
            GwsAccountConfig(
                alias="home",
                email="home@example.test",
                config_dir=Path("/tmp/home"),
                binding_state="verified",
            ),
            runner=Runner(),
        )
        with self.assertRaises(GwsMailIdentityError) as caught:
            client.verify_identity()
        self.assertEqual(caught.exception.code, "gws_identity_mismatch")
        self.assertEqual(
            str(caught.exception),
            "The selected GWS profile identity does not match the configured account.",
        )
        runtime = MailRuntime(
            project_index=Path("/tmp/nonexistent-project-index"),
            config_root=Path("/tmp/nonexistent-mail-config"),
        )
        self.assertEqual(
            safe_error_payload(runtime, caught.exception),
            {
                "code": "gws_identity_mismatch",
                "message": (
                    "The selected GWS profile identity does not match the configured "
                    "account."
                ),
            },
        )


class ProtonSessionTests(unittest.TestCase):
    def account(
        self, root: Path, *, state: str = "verified"
    ) -> ProtonBridgeAccountConfig:
        write_proton_config(root)
        return ProtonBridgeAccountConfig(
            alias="work",
            email="work@example.test",
            local_config_path=root / "proton" / "work.toml",
            binding_state=state,
        )

    def client(
        self,
        root: Path,
        events: list[object],
        backend: FakeSecretBackend,
        *,
        certificate: bytes = b"certificate",
    ) -> ProtonBridgeMailClient:
        resolver = MemoryCachingSecretResolver(backend)
        return ProtonBridgeMailClient(
            self.account(root),
            secret_resolver=resolver,
            imap_factory=lambda host, port, timeout: FakeImap(
                host,
                port,
                timeout=timeout,
                events=events,
                certificate=certificate,
            ),
        )

    def test_tls_pin_precedes_secret_and_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events: list[object] = []
            backend = FakeSecretBackend(events)
            self.assertTrue(self.client(Path(tmp), events, backend).status())

        self.assertLess(events.index("starttls"), events.index("certificate"))
        self.assertLess(events.index("certificate"), events.index("secret"))
        login = next(
            index
            for index, event in enumerate(events)
            if isinstance(event, tuple) and event[0] == "login"
        )
        self.assertLess(events.index("secret"), login)

    def test_pin_mismatch_never_resolves_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[object] = []
            backend = FakeSecretBackend(events)
            with self.assertRaises(ProtonBridgeIdentityError):
                self.client(root, events, backend, certificate=b"wrong").status()
        self.assertEqual(backend.calls, [])

    def test_only_readonly_select_search_and_header_peek_are_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events: list[object] = []
            backend = FakeSecretBackend(events)
            result = self.client(Path(tmp), events, backend).search(
                query="",
                after="2026-07-01",
                before="2026-07-20",
                max_results=1,
            )
        self.assertEqual(len(result), 1)
        self.assertIn(("select", "INBOX", True), events)
        transcript = repr(events).upper()
        self.assertIn("SEARCH", transcript)
        self.assertIn("FETCH", transcript)
        self.assertIn(HEADER_FETCH_ITEM, transcript)
        for forbidden in ("STORE", "MOVE", "COPY", "EXPUNGE", "SMTP", "BODY[]"):
            self.assertNotIn(forbidden, transcript)

    def test_selected_content_matches_schema_and_attachment_is_explicit(
        self,
    ) -> None:
        structure = (
            b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "BASE64" 20 1 '
            b"NIL NIL NIL NIL)"
            b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 4 '
            b'NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL) '
            b'"MIXED" ("BOUNDARY" "x") NIL NIL NIL)'
        )

        class ContentImap(FakeImap):
            def uid(self, command: str, *args):
                if command == "FETCH":
                    uid = int(str(args[0]))
                    item = str(args[1])
                    self.events.append(("uid", command, *args))
                    if item == BODYSTRUCTURE_FETCH_ITEM:
                        return "OK", [
                            f"1 (UID {uid} BODYSTRUCTURE ".encode("ascii")
                            + structure
                            + b")"
                        ]
                    match = re.fullmatch(r"\(UID BODY\.PEEK\[([0-9.]+)\]\)", item)
                    if match:
                        part = match.group(1)
                        literal = {
                            "1": b"SGVsbG8gZnJvbSBBcnM=",
                            "2": b"UERG",
                        }[part]
                        descriptor = (
                            f"1 (UID {uid} BODY[{part}] {{{len(literal)}}}"
                        ).encode("ascii")
                        return "OK", [(descriptor, literal), b")"]
                return super().uid(command, *args)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events: list[object] = []
            backend = FakeSecretBackend(events)
            client = ProtonBridgeMailClient(
                self.account(root),
                secret_resolver=MemoryCachingSecretResolver(backend),
                imap_factory=lambda host, port, timeout: ContentImap(
                    host,
                    port,
                    timeout=timeout,
                    events=events,
                    certificate=b"certificate",
                ),
            )
            message_id = _encode_message_id(7, 1)
            content = client.get_content(message_id, max_bytes=1024).to_dict()
            self.assertEqual(content["content"]["text"], "Hello from Ars")
            self.assertTrue(content["untrusted"])
            attachment_id = content["attachments"][0]["id"]
            before_attachment = repr(events)
            self.assertNotIn("BODY.PEEK[2]", before_attachment)
            self.assertEqual(
                client.get_attachment(message_id, attachment_id, max_bytes=10),
                b"PDF",
            )
            self.assertIn("BODY.PEEK[2]", repr(events))

    def test_planned_binding_blocks_normal_read_before_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_proton_config(root)
            backend = FakeSecretBackend()
            client = ProtonBridgeMailClient(
                self.account(root, state="planned"),
                secret_resolver=MemoryCachingSecretResolver(backend),
            )
            with self.assertRaises(ProtonBridgePolicyError):
                client.search(
                    query="",
                    after="2026-07-01",
                    before="2026-07-20",
                    max_results=1,
                )
        self.assertEqual(backend.calls, [])

    def test_secret_is_resolved_once_per_process_and_again_after_restart(self) -> None:
        backend = FakeSecretBackend()
        service = f"{KEYCHAIN_SERVICE_PREFIX}work"
        first_process = MemoryCachingSecretResolver(backend)
        for _ in range(3):
            self.assertEqual(
                first_process.resolve(
                    service=service,
                    account="work@example.test",
                    timeout_seconds=10,
                ),
                "bridge-secret",
            )
        self.assertEqual(len(backend.calls), 1)

        second_process = MemoryCachingSecretResolver(backend)
        second_process.resolve(
            service=service,
            account="work@example.test",
            timeout_seconds=10,
        )
        self.assertEqual(len(backend.calls), 2)

    def test_proton_bounds_reject_selector_large_window_and_result_limit(self) -> None:
        with self.assertRaises(ProtonBridgePolicyError):
            _build_search_criteria(
                query="from:anyone",
                after="2026-07-01",
                before="2026-07-20",
            )
        with self.assertRaises(ProtonBridgePolicyError):
            _build_search_criteria(
                query="",
                after="2026-06-01",
                before="2026-07-20",
            )
        for value in (0, 101, True):
            with self.subTest(value=value):
                with self.assertRaises(ProtonBridgePolicyError):
                    _validate_max_results(value)

    def test_keychain_lookup_uses_fixed_binary_without_secret_in_argv_or_env(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess(
            args=["security"], returncode=0, stdout="bridge-secret\n", stderr=""
        )
        resolver = MacOSKeychainSecretResolver()
        with patch("ars_operandi_mail.proton.sys.platform", "darwin"):
            with patch.object(Path, "is_file", return_value=True):
                with patch(
                    "ars_operandi_mail.proton.subprocess.run", return_value=completed
                ) as run:
                    secret = resolver.resolve(
                        service=f"{KEYCHAIN_SERVICE_PREFIX}work",
                        account="work@example.test",
                        timeout_seconds=10,
                    )
        self.assertEqual(secret, "bridge-secret")
        self.assertNotIn("bridge-secret", repr(run.call_args.args))
        self.assertNotIn("bridge-secret", repr(run.call_args.kwargs["env"]))

    def test_malformed_provider_config_fails_before_keychain_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_proton_config(root)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'host = "127.0.0.1"', 'host = "0.0.0.0"'
                ),
                encoding="utf-8",
            )
            backend = FakeSecretBackend()
            network_calls: list[object] = []
            client = ProtonBridgeMailClient(
                ProtonBridgeAccountConfig(
                    alias="work",
                    email="work@example.test",
                    local_config_path=config_path,
                    binding_state="verified",
                ),
                secret_resolver=MemoryCachingSecretResolver(backend),
                imap_factory=lambda *args, **kwargs: network_calls.append(
                    (args, kwargs)
                ),
            )
            with self.assertRaises(ProtonBridgeConfigError):
                client.status()
        self.assertEqual(backend.calls, [])
        self.assertEqual(network_calls, [])

    def test_proton_headers_neutralize_controls_and_drop_unallowlisted_fields(
        self,
    ) -> None:
        metadata = _metadata_from_headers(
            b"From: A\x00B <sender@example.test>\r\n"
            b"To: work@example.test\r\n"
            b"Subject: hello\x00world\r\n"
            b"Date: Mon, 20 Jul 2026 10:00:00 +0200\r\n"
            b"Received: private-hop\r\n\r\n",
            message_id="pb1_example",
        )
        payload = metadata.to_dict()
        self.assertEqual(set(payload), {"id", "from", "to", "subject", "date"})
        self.assertNotIn("private-hop", repr(payload))
        self.assertEqual(payload["subject"], "hello world")
        self.assertFalse(
            any(
                ord(character) < 32 or ord(character) == 127
                for character in repr(payload)
            )
        )


class FakeMailClient:
    def __init__(self, alias: str) -> None:
        self.alias = alias

    def status(self):
        return True

    def onboarding_probe(self, **kwargs):
        return OnboardingProbe(self.alias, True, 1)

    def search(self, **kwargs):
        return (MessageMetadata("opaque", "from", "to", "subject", "date"),)

    def get_metadata(self, message_id: str):
        return MessageMetadata(message_id, "from", "to", "subject", "date")

    def get_content(self, message_id: str, *, max_bytes: int):
        return MessageContent(
            message_id,
            "from",
            "to",
            "subject",
            "date",
            "text",
            "<p>html</p>",
            False,
            False,
            (AttachmentMetadata("part", "file.bin", "application/octet-stream", 4),),
        )

    def get_attachment(self, message_id: str, attachment_id: str, *, max_bytes: int):
        return b"safe"

    def auth(self):
        return None


class RuntimeAndMcpTests(unittest.TestCase):
    def runtime(self, root: Path) -> MailRuntime:
        return MailRuntime(
            project_index=write_index(root),
            config_root=root / "local",
            gws_factory=lambda account: FakeMailClient(account.alias),
            proton_factory=lambda account, resolver: FakeMailClient(account.alias),
        )

    def test_runtime_routes_each_operation_and_blocks_planned_normal_reads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root)
            self.assertEqual(runtime.status(account="work")["provider"], "proton")
            self.assertEqual(
                runtime.search(
                    project="personal/example",
                    after="2026-07-01",
                    before="2026-07-20",
                    max_results=1,
                )["provider"],
                "gws",
            )
            write_index(root, state="planned")
            with self.assertRaisesRegex(MailConfigError, "not verified"):
                runtime.search(
                    account="work",
                    after="2026-07-01",
                    before="2026-07-20",
                )

    def test_mcp_has_only_seven_read_only_mail_tools_and_no_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = build_mcp(self.runtime(Path(tmp)))
            tools = asyncio.run(server.list_tools())

        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "mail_accounts",
                "mail_status",
                "mail_onboarding",
                "mail_search",
                "mail_metadata",
                "mail_content",
                "mail_attachment",
            },
        )
        for tool in tools:
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)

    def test_runtime_selected_content_and_attachment_are_explicit_and_bounded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self.runtime(root)
            content = runtime.content(
                account="home", message_id="opaque", max_bytes=1024
            )
            self.assertTrue(content["message"]["untrusted"])
            target = root / "selected.bin"
            receipt = runtime.attachment(
                account="work",
                message_id="opaque",
                attachment_id="part",
                output_path=target,
                max_bytes=4,
            )
            self.assertEqual(target.read_bytes(), b"safe")
            self.assertEqual(receipt["byte_count"], 4)
            self.assertNotIn("output_path", receipt)

    def test_success_payload_preserves_selected_content_without_error_redaction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self.runtime(Path(tmp))
            text = ("Line one\nLine two password=mail-value " * 80).strip()
            html = "<p>" + ("Selected body " * 160) + "</p>"
            payload = {
                "message": {
                    "content": {
                        "text": text,
                        "html": html,
                        "text_truncated": False,
                        "html_truncated": False,
                    }
                }
            }

            result = safe_tool_call(runtime, lambda: payload)

        self.assertEqual(result["message"]["content"]["text"], text)
        self.assertEqual(result["message"]["content"]["html"], html)
        self.assertFalse(result["message"]["content"]["text_truncated"])
        self.assertFalse(result["message"]["content"]["html_truncated"])

    def test_process_runtime_reuses_one_keychain_resolution_across_operations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_root = root / "local"
            write_index(root)
            write_proton_config(config_root)
            backend = FakeSecretBackend()

            def runtime() -> MailRuntime:
                return MailRuntime(
                    project_index=root / "projects.yaml",
                    config_root=config_root,
                    keychain_backend=backend,
                    proton_factory=lambda account, resolver: ProtonBridgeMailClient(
                        account,
                        secret_resolver=resolver,
                        imap_factory=lambda host, port, timeout: FakeImap(
                            host,
                            port,
                            timeout=timeout,
                            events=[],
                            certificate=b"certificate",
                        ),
                    ),
                )

            first_process = runtime()
            first_process.status(account="work")
            first_process.status(account="work")
            self.assertEqual(len(backend.calls), 1)

            second_process = runtime()
            second_process.status(account="work")
            self.assertEqual(len(backend.calls), 2)

    def test_protocol_error_redacts_cached_secret_and_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = FakeSecretBackend()
            runtime = MailRuntime(
                project_index=write_index(root),
                config_root=root / "local",
                keychain_backend=backend,
            )
            runtime.keychain.resolve(
                service=f"{KEYCHAIN_SERVICE_PREFIX}work",
                account="work@example.test",
                timeout_seconds=10,
            )
            result = safe_tool_call(
                runtime,
                lambda: (_ for _ in ()).throw(
                    ProtonBridgeMailError(
                        "provider failure\r\npassword=bridge-secret\x00tail"
                    )
                ),
            )
        rendered = repr(result)
        self.assertNotIn("bridge-secret", rendered)
        self.assertNotIn("\r", result["error"]["message"])
        self.assertNotIn("\n", result["error"]["message"])
        self.assertNotIn("\x00", result["error"]["message"])


if __name__ == "__main__":
    unittest.main()
