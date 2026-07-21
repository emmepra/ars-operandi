from __future__ import annotations

import base64
import hashlib
import hmac
import imaplib
import re
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from .config import ProtonBridgeAccountConfig
from .gws import MessageMetadata, OnboardingProbe


BRIDGE_HOST = "127.0.0.1"
BRIDGE_TRANSPORT = "starttls"
IDENTITY_MODES = frozenset({"split-address", "dedicated-account"})
LOCAL_CONFIG_KEYS = frozenset(
    {
        "host",
        "port",
        "transport",
        "tls_cert_sha256",
        "identity_mode",
        "bridge_username",
        "keychain_service",
        "keychain_account",
    }
)
# The service namespace is a stable credential reference already used by the
# consumer. Keeping it avoids copying or re-authorizing the Bridge password;
# it does not confer runtime ownership on the former repository.
KEYCHAIN_SERVICE_PREFIX = "workflow-agent.mailctl.proton-bridge."
CONNECT_TIMEOUT_SECONDS = 3.0
COMMAND_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 30.0
MAX_WINDOW_DAYS = 31
MAX_RESULTS = 100
MAX_MATCHED_UIDS = 1000
MAX_HEADER_VALUE_LENGTH = 1000
MAX_SECRET_LENGTH = 4096
MAX_UID = 4_294_967_295
HEADER_NAMES = ("From", "To", "Subject", "Date")
HEADER_FETCH_ITEM = "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])"
MESSAGE_ID_PREFIX = "pb1_"


class ProtonBridgeMailError(RuntimeError):
    """Raised when the bounded Proton Bridge runtime cannot complete."""


class ProtonBridgeConfigError(ProtonBridgeMailError):
    """Raised before network or credential access for invalid local config."""


class ProtonBridgePolicyError(ProtonBridgeMailError):
    """Raised before a forbidden or unbounded Proton Bridge operation."""


class ProtonBridgeIdentityError(ProtonBridgeMailError):
    """Raised when the selected Bridge identity cannot be proven exactly."""


@dataclass(frozen=True)
class ProtonBridgeLocalConfig:
    host: str
    port: int
    transport: str
    tls_cert_sha256: str
    identity_mode: str
    bridge_username: str
    keychain_service: str
    keychain_account: str


@dataclass(frozen=True)
class _BridgeSession:
    client: Any
    deadline: _Deadline
    uidvalidity: int


class _Deadline:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._started = clock()

    def connect_timeout(self) -> float:
        return min(CONNECT_TIMEOUT_SECONDS, self._remaining())

    def command_timeout(self) -> float:
        return min(COMMAND_TIMEOUT_SECONDS, self._remaining())

    def check(self) -> None:
        self._remaining()

    def cleanup_timeout(self) -> float | None:
        remaining = TOTAL_TIMEOUT_SECONDS - (self._clock() - self._started)
        return min(1.0, remaining) if remaining > 0 else None

    def _remaining(self) -> float:
        remaining = TOTAL_TIMEOUT_SECONDS - (self._clock() - self._started)
        if remaining <= 0:
            raise ProtonBridgeMailError("Proton Bridge operation timed out.")
        return remaining


def load_proton_bridge_config(
    account: ProtonBridgeAccountConfig,
) -> ProtonBridgeLocalConfig:
    path = account.local_config_path
    if not path.is_file():
        raise ProtonBridgeConfigError("Proton Bridge local config is unavailable.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ProtonBridgeConfigError(
            "Proton Bridge local config could not be loaded."
        ) from None
    if not isinstance(raw, dict) or set(raw) != LOCAL_CONFIG_KEYS:
        raise ProtonBridgeConfigError("Proton Bridge local config fields are invalid.")

    host = raw.get("host")
    port = raw.get("port")
    transport = raw.get("transport")
    pin = raw.get("tls_cert_sha256")
    identity_mode = raw.get("identity_mode")
    username = raw.get("bridge_username")
    service = raw.get("keychain_service")
    keychain_account = raw.get("keychain_account")
    expected_service = f"{KEYCHAIN_SERVICE_PREFIX}{account.alias}"

    if host != BRIDGE_HOST:
        raise ProtonBridgeConfigError("Proton Bridge host must be literal localhost.")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ProtonBridgeConfigError("Proton Bridge IMAP port is invalid.")
    if transport != BRIDGE_TRANSPORT:
        raise ProtonBridgeConfigError("Proton Bridge requires STARTTLS.")
    if not isinstance(pin, str) or re.fullmatch(r"[0-9A-Fa-f]{64}", pin) is None:
        raise ProtonBridgeConfigError("Proton Bridge TLS certificate pin is invalid.")
    if identity_mode not in IDENTITY_MODES:
        raise ProtonBridgeConfigError("Proton Bridge identity mode is invalid.")
    if username != account.email:
        raise ProtonBridgeIdentityError(
            "Proton Bridge username does not match expected identity."
        )
    if service != expected_service or keychain_account != account.email:
        raise ProtonBridgeConfigError(
            "Proton Bridge Keychain reference is not dedicated."
        )
    return ProtonBridgeLocalConfig(
        BRIDGE_HOST,
        int(port),
        BRIDGE_TRANSPORT,
        pin.lower(),
        str(identity_mode),
        account.email,
        expected_service,
        account.email,
    )


class SecretResolver(Protocol):
    def resolve(self, *, service: str, account: str, timeout_seconds: float) -> str: ...


class SecretRedactor(Protocol):
    def register(self, value: str) -> None: ...


class MacOSKeychainSecretResolver:
    """Resolve one Bridge IMAP password from a dedicated Keychain item."""

    executable = Path("/usr/bin/security")

    def resolve(self, *, service: str, account: str, timeout_seconds: float) -> str:
        if sys.platform != "darwin" or not self.executable.is_file():
            raise ProtonBridgeMailError("macOS Keychain is unavailable.")
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "find-generic-password",
                    "-w",
                    "-s",
                    service,
                    "-a",
                    account,
                ],
                env={"PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise ProtonBridgeMailError(
                "Proton Bridge Keychain lookup timed out."
            ) from None
        except OSError:
            raise ProtonBridgeMailError(
                "Proton Bridge Keychain lookup failed."
            ) from None
        if completed.returncode != 0:
            raise ProtonBridgeMailError("Proton Bridge Keychain lookup failed.")
        secret = (completed.stdout or "").rstrip("\r\n")
        if (
            not secret
            or len(secret) > MAX_SECRET_LENGTH
            or any(ord(character) < 32 for character in secret)
        ):
            raise ProtonBridgeMailError("Proton Bridge Keychain item is invalid.")
        return secret


class MemoryCachingSecretResolver:
    """Resolve each dedicated Keychain item once per process and keep it in RAM."""

    def __init__(
        self,
        backend: SecretResolver | None = None,
        *,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.backend = backend or MacOSKeychainSecretResolver()
        self.redactor = redactor
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def resolve(self, *, service: str, account: str, timeout_seconds: float) -> str:
        key = (service, account)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            try:
                secret = self.backend.resolve(
                    service=service,
                    account=account,
                    timeout_seconds=timeout_seconds,
                )
            except ProtonBridgeMailError:
                raise
            except Exception:
                raise ProtonBridgeMailError(
                    "Proton Bridge Keychain lookup failed."
                ) from None
            if (
                not isinstance(secret, str)
                or not secret
                or len(secret) > MAX_SECRET_LENGTH
                or any(ord(character) < 32 for character in secret)
            ):
                raise ProtonBridgeMailError("Proton Bridge Keychain item is invalid.")
            if self.redactor is not None:
                self.redactor.register(secret)
            self._cache[key] = secret
            return secret


class ProtonBridgeMailClient:
    """Provider-specific, read-only Proton Mail Bridge metadata client."""

    def __init__(
        self,
        account: ProtonBridgeAccountConfig,
        *,
        secret_resolver: SecretResolver | None = None,
        imap_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if account.provider != "proton":
            raise ProtonBridgePolicyError(
                "Proton Bridge requires an explicit proton profile."
            )
        self.account = account
        self.secret_resolver = secret_resolver or MemoryCachingSecretResolver()
        self.imap_factory = imap_factory or imaplib.IMAP4
        self.clock = clock

    def auth(self) -> None:
        raise ProtonBridgePolicyError(
            "Proton Bridge authentication is external to mailctl."
        )

    def status(self) -> bool:
        with self._session():
            return True

    def onboarding_probe(
        self, *, query: str, after: str, before: str
    ) -> OnboardingProbe:
        criteria = _build_search_criteria(query=query, after=after, before=before)
        with self._session() as session:
            uids = self._search_uids(session, criteria)
        return OnboardingProbe(self.account.alias, True, min(len(uids), 1))

    def search(
        self, *, query: str, after: str, before: str, max_results: int
    ) -> tuple[MessageMetadata, ...]:
        self._require_verified_binding()
        _validate_max_results(max_results)
        criteria = _build_search_criteria(query=query, after=after, before=before)
        with self._session() as session:
            uids = self._search_uids(session, criteria)
            selected = tuple(reversed(uids[-max_results:]))
            return tuple(self._fetch_metadata(session, uid) for uid in selected)

    def get_metadata(self, message_id: str) -> MessageMetadata:
        self._require_verified_binding()
        expected_uidvalidity, uid = _decode_message_id(message_id)
        with self._session() as session:
            if session.uidvalidity != expected_uidvalidity:
                raise ProtonBridgePolicyError("Proton Bridge message id is stale.")
            return self._fetch_metadata(session, uid)

    def _require_verified_binding(self) -> None:
        if not self.account.is_verified:
            raise ProtonBridgePolicyError(
                "Normal Proton Bridge reads require a verified mail binding."
            )

    @contextmanager
    def _session(self) -> Iterator[_BridgeSession]:
        config = load_proton_bridge_config(self.account)
        deadline = _Deadline(self.clock)
        client: Any | None = None
        try:
            client = self.imap_factory(
                config.host, config.port, timeout=deadline.connect_timeout()
            )
            if hasattr(client, "debug"):
                client.debug = 0
            capabilities = self._command(client, deadline, client.capability)
            if "STARTTLS" not in _capability_tokens(capabilities):
                raise ProtonBridgePolicyError(
                    "Proton Bridge does not advertise STARTTLS."
                )
            self._command(client, deadline, client.starttls, _pinned_tls_context())
            certificate = _peer_certificate(client)
            actual_pin = hashlib.sha256(certificate).hexdigest()
            if not hmac.compare_digest(actual_pin, config.tls_cert_sha256):
                raise ProtonBridgeIdentityError(
                    "Proton Bridge TLS certificate pin mismatch."
                )

            secret = self.secret_resolver.resolve(
                service=config.keychain_service,
                account=config.keychain_account,
                timeout_seconds=deadline.command_timeout(),
            )
            deadline.check()
            try:
                self._command(
                    client, deadline, client.login, config.bridge_username, secret
                )
            finally:
                secret = ""
            self._command(client, deadline, client.select, "INBOX", readonly=True)
            yield _BridgeSession(client, deadline, _read_uidvalidity(client))
            deadline.check()
        except ProtonBridgeMailError:
            raise
        except Exception:
            raise ProtonBridgeMailError("Proton Bridge operation failed.") from None
        finally:
            if client is not None:
                _safe_logout(client, deadline)

    def _command(
        self,
        client: Any,
        deadline: _Deadline,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        sock = getattr(client, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(deadline.command_timeout())
        try:
            result = callback(*args, **kwargs)
        except (OSError, TimeoutError, ssl.SSLError, imaplib.IMAP4.error):
            raise ProtonBridgeMailError("Proton Bridge command failed.") from None
        deadline.check()
        _require_ok(result)
        return result

    def _search_uids(
        self, session: _BridgeSession, criteria: tuple[str, ...]
    ) -> tuple[int, ...]:
        response = self._command(
            session.client,
            session.deadline,
            session.client.uid,
            "SEARCH",
            None,
            *criteria,
        )
        data = response[1]
        raw = data[0] if isinstance(data, (list, tuple)) and data else b""
        uids = _parse_uid_search(raw)
        if len(uids) > MAX_MATCHED_UIDS:
            raise ProtonBridgePolicyError(
                "Proton Bridge search matched too many messages."
            )
        return uids

    def _fetch_metadata(self, session: _BridgeSession, uid: int) -> MessageMetadata:
        response = self._command(
            session.client,
            session.deadline,
            session.client.uid,
            "FETCH",
            str(uid),
            HEADER_FETCH_ITEM,
        )
        headers = _extract_header_literal(response[1], expected_uid=uid)
        return _metadata_from_headers(
            headers, message_id=_encode_message_id(session.uidvalidity, uid)
        )


def _pinned_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _peer_certificate(client: Any) -> bytes:
    sock = getattr(client, "sock", None)
    if sock is None or not hasattr(sock, "getpeercert"):
        raise ProtonBridgeIdentityError("Proton Bridge TLS peer is unavailable.")
    certificate = sock.getpeercert(binary_form=True)
    if not isinstance(certificate, bytes) or not certificate:
        raise ProtonBridgeIdentityError(
            "Proton Bridge TLS peer certificate is unavailable."
        )
    return certificate


def _capability_tokens(response: Any) -> frozenset[str]:
    _require_ok(response)
    data = response[1]
    if not isinstance(data, (list, tuple)):
        raise ProtonBridgeMailError("Proton Bridge capability response is invalid.")
    tokens: set[str] = set()
    for item in data:
        if isinstance(item, bytes):
            try:
                tokens.update(item.decode("ascii").upper().split())
            except UnicodeDecodeError:
                raise ProtonBridgeMailError(
                    "Proton Bridge capability response is invalid."
                ) from None
    return frozenset(tokens)


def _require_ok(response: Any) -> None:
    if (
        not isinstance(response, tuple)
        or len(response) != 2
        or not isinstance(response[0], str)
        or response[0].upper() != "OK"
    ):
        raise ProtonBridgeMailError("Proton Bridge command was rejected.")


def _read_uidvalidity(client: Any) -> int:
    response = client.response("UIDVALIDITY")
    if not isinstance(response, tuple) or len(response) != 2:
        raise ProtonBridgeMailError("Proton Bridge UIDVALIDITY is unavailable.")
    data = response[1]
    if not isinstance(data, (list, tuple)) or len(data) != 1:
        raise ProtonBridgeMailError("Proton Bridge UIDVALIDITY is unavailable.")
    raw = data[0]
    if isinstance(raw, bytes):
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise ProtonBridgeMailError(
                "Proton Bridge UIDVALIDITY is invalid."
            ) from None
    elif isinstance(raw, str):
        text = raw
    else:
        raise ProtonBridgeMailError("Proton Bridge UIDVALIDITY is invalid.")
    if re.fullmatch(r"[1-9][0-9]*", text) is None or int(text) > MAX_UID:
        raise ProtonBridgeMailError("Proton Bridge UIDVALIDITY is invalid.")
    return int(text)


def _build_search_criteria(*, query: str, after: str, before: str) -> tuple[str, ...]:
    if query.strip():
        raise ProtonBridgePolicyError(
            "Proton Bridge does not accept free-form or raw search queries."
        )
    start = _parse_date(after, option="after")
    end = _parse_date(before, option="before")
    if start >= end:
        raise ProtonBridgePolicyError("Proton Bridge search window is invalid.")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ProtonBridgePolicyError("Proton Bridge search window exceeds 31 days.")
    return (
        "SINCE",
        (start + timedelta(days=1)).strftime("%d-%b-%Y"),
        "BEFORE",
        end.strftime("%d-%b-%Y"),
    )


def _parse_date(value: str, *, option: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ProtonBridgePolicyError(
            f"Proton Bridge {option} bound must use YYYY-MM-DD."
        ) from exc


def _validate_max_results(max_results: int) -> None:
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= MAX_RESULTS
    ):
        raise ProtonBridgePolicyError("Proton Bridge max-results must be finite.")


def _parse_uid_search(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, bytes):
        raise ProtonBridgeMailError("Proton Bridge search response is invalid.")
    if not raw.strip():
        return ()
    try:
        tokens = raw.decode("ascii").split()
    except UnicodeDecodeError:
        raise ProtonBridgeMailError(
            "Proton Bridge search response is invalid."
        ) from None
    if any(re.fullmatch(r"[1-9][0-9]*", token) is None for token in tokens):
        raise ProtonBridgeMailError("Proton Bridge search response is invalid.")
    values = tuple(int(token) for token in tokens)
    if any(value > MAX_UID for value in values) or len(set(values)) != len(values):
        raise ProtonBridgeMailError("Proton Bridge search response is invalid.")
    return tuple(sorted(values))


def _extract_header_literal(data: object, *, expected_uid: int) -> bytes:
    if not isinstance(data, (list, tuple)):
        raise ProtonBridgeMailError("Proton Bridge metadata response is invalid.")
    literals: list[bytes] = []
    pattern = re.compile(rb"\bUID\s+" + str(expected_uid).encode("ascii") + rb"\b")
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        descriptor, literal = item
        if not isinstance(descriptor, bytes) or not isinstance(literal, bytes):
            continue
        upper = descriptor.upper()
        if pattern.search(upper) and b"BODY[HEADER.FIELDS" in upper:
            literals.append(literal)
    if len(literals) != 1:
        raise ProtonBridgeMailError("Proton Bridge metadata response is invalid.")
    return literals[0]


def _metadata_from_headers(raw: bytes, *, message_id: str) -> MessageMetadata:
    try:
        parsed = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    except Exception:
        raise ProtonBridgeMailError(
            "Proton Bridge metadata response is invalid."
        ) from None
    values: dict[str, str] = {}
    for name in HEADER_NAMES:
        headers = parsed.get_all(name, [])
        values[name] = _sanitize_header_value(str(headers[0]) if headers else "")
    return MessageMetadata(
        message_id,
        values["From"],
        values["To"],
        values["Subject"],
        values["Date"],
    )


def _sanitize_header_value(value: str) -> str:
    neutralized = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in value
    )
    return " ".join(neutralized.split())[:MAX_HEADER_VALUE_LENGTH]


def _encode_message_id(uidvalidity: int, uid: int) -> str:
    token = base64.urlsafe_b64encode(f"{uidvalidity}:{uid}".encode("ascii")).decode(
        "ascii"
    )
    return f"{MESSAGE_ID_PREFIX}{token.rstrip('=')}"


def _decode_message_id(message_id: str) -> tuple[int, int]:
    if (
        not isinstance(message_id, str)
        or not message_id.startswith(MESSAGE_ID_PREFIX)
        or len(message_id) > 128
    ):
        raise ProtonBridgePolicyError("Proton Bridge message id is invalid.")
    token = message_id.removeprefix(MESSAGE_ID_PREFIX)
    if not token or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise ProtonBridgePolicyError("Proton Bridge message id is invalid.")
    try:
        decoded = base64.b64decode(
            token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
        ).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtonBridgePolicyError("Proton Bridge message id is invalid.") from exc
    match = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*)", decoded)
    if match is None:
        raise ProtonBridgePolicyError("Proton Bridge message id is invalid.")
    uidvalidity, uid = (int(part) for part in match.groups())
    if uidvalidity > MAX_UID or uid > MAX_UID:
        raise ProtonBridgePolicyError("Proton Bridge message id is invalid.")
    return uidvalidity, uid


def _safe_logout(client: Any, deadline: _Deadline) -> None:
    try:
        timeout = deadline.cleanup_timeout()
        if timeout is None:
            return
        sock = getattr(client, "sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout)
        client.logout()
    except Exception:
        return
