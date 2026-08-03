from __future__ import annotations

import asyncio
import hashlib
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
    GwsMailClient,
    GwsMailIdentityError,
    GwsMailPolicyError,
    MESSAGE_GET_METHOD,
    MessageMetadata,
    OnboardingProbe,
    build_query,
    clean_gws_environment,
    validate_gmail_request,
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
from ars_operandi_mail.service import MailRuntime, build_mcp, safe_tool_call


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
    def test_selected_content_is_normalized_and_attachment_is_explicit(self) -> None:
        calls: list[str] = []

        class Runner:
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
        with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/x"}):
            with self.assertRaises(GwsMailPolicyError):
                clean_gws_environment(Path("/tmp/gws"))

    def test_exact_gws_identity_mismatch_fails_closed(self) -> None:
        class Runner:
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
        with self.assertRaises(GwsMailIdentityError):
            client.verify_identity()


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
