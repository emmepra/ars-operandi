from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ALLOWED_PROVIDERS = frozenset({"gws", "proton"})
ALLOWED_BINDING_STATES = frozenset({"planned", "verified"})


class MailConfigError(RuntimeError):
    """Raised when the explicit mail routing manifest is invalid."""


@dataclass(frozen=True)
class GwsAccountConfig:
    alias: str
    email: str
    config_dir: Path
    binding_state: str
    provider: str = "gws"

    @property
    def is_verified(self) -> bool:
        return self.binding_state == "verified"


@dataclass(frozen=True)
class ProtonBridgeAccountConfig:
    alias: str
    email: str
    local_config_path: Path
    binding_state: str
    provider: str = "proton"

    @property
    def is_verified(self) -> bool:
        return self.binding_state == "verified"


MailAccountConfig = GwsAccountConfig | ProtonBridgeAccountConfig


@dataclass(frozen=True)
class MailConfig:
    path: Path
    config_root: Path
    accounts: dict[str, MailAccountConfig]
    profile_adapters: dict[str, str]
    project_profiles: dict[str, str]

    def get_account(
        self, alias: str, *, require_verified: bool = True
    ) -> MailAccountConfig:
        if alias not in self.profile_adapters:
            raise MailConfigError("Unknown mail profile alias.")
        account = self.accounts.get(alias)
        if account is None or account.provider not in ALLOWED_PROVIDERS:
            raise MailConfigError("Mail profile provider is unsupported.")
        if require_verified and not account.is_verified:
            raise MailConfigError("Mail profile binding is not verified.")
        return account

    def get_project_account(
        self, project_key: str, *, require_verified: bool = True
    ) -> MailAccountConfig:
        profile = self.project_profiles.get(project_key)
        if profile is None:
            raise MailConfigError("Project has no verified mail route.")
        return self.get_account(profile, require_verified=require_verified)

    def resolve(
        self,
        *,
        alias: str | None = None,
        project_key: str | None = None,
        require_verified: bool = True,
    ) -> MailAccountConfig:
        if bool(alias) == bool(project_key):
            raise MailConfigError(
                "Select exactly one mail profile alias or project key."
            )
        if alias:
            return self.get_account(alias, require_verified=require_verified)
        return self.get_project_account(
            str(project_key), require_verified=require_verified
        )


def load_mail_config(
    project_index_path: str | Path, *, config_root: str | Path
) -> MailConfig:
    """Load the sole routing manifest from explicitly supplied paths."""

    path = Path(project_index_path).expanduser().resolve()
    root = Path(config_root).expanduser().resolve()
    if not path.is_file():
        raise MailConfigError("Project Index is unavailable.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise MailConfigError("Project Index could not be loaded.") from exc
    if not isinstance(raw, dict):
        raise MailConfigError("Project Index must be a mapping.")

    accounts, adapters = _load_profiles(raw, config_root=root)
    projects = _load_project_routes(raw, profile_adapters=adapters)
    return MailConfig(path, root, accounts, adapters, projects)


def _load_profiles(
    raw: dict[str, Any], *, config_root: Path
) -> tuple[dict[str, MailAccountConfig], dict[str, str]]:
    profiles = raw.get("mail_profiles")
    connections = raw.get("mail_connections")
    if not isinstance(profiles, dict) or not profiles:
        raise MailConfigError("Project Index mail profiles are missing.")
    if not isinstance(connections, dict) or not connections:
        raise MailConfigError("Project Index mail connections are missing.")

    accounts: dict[str, MailAccountConfig] = {}
    adapters: dict[str, str] = {}
    used_connections: set[str] = set()
    expected_emails: set[str] = set()
    provider_paths: set[Path] = set()

    for raw_alias, raw_profile in profiles.items():
        alias = _required_alias(raw_alias)
        if not isinstance(raw_profile, dict):
            raise MailConfigError("Mail profile definition is invalid.")
        connection_ids = raw_profile.get("connections")
        if (
            not isinstance(connection_ids, list)
            or len(connection_ids) != 1
            or not isinstance(connection_ids[0], str)
            or not connection_ids[0].strip()
        ):
            raise MailConfigError(
                "Each mail profile must select exactly one connection."
            )
        connection_id = connection_ids[0].strip()
        if connection_id in used_connections:
            raise MailConfigError("Mail profiles must not share a connection.")
        used_connections.add(connection_id)
        connection = connections.get(connection_id)
        if not isinstance(connection, dict):
            raise MailConfigError("Mail profile references an unavailable connection.")
        if connection.get("account_alias") != alias:
            raise MailConfigError(
                "Mail profile alias and connection alias do not match."
            )

        provider = connection.get("adapter")
        if provider not in ALLOWED_PROVIDERS:
            raise MailConfigError("Mail connection provider is unsupported.")
        if connection.get("execution_host") != "local-mac":
            raise MailConfigError(
                "Mail connection must use the local Mac execution host."
            )
        if connection.get("access_mode") != "read-only":
            raise MailConfigError("Mail connection must be read-only.")
        state = connection.get("binding_state")
        if state not in ALLOWED_BINDING_STATES:
            raise MailConfigError("Mail connection binding state is invalid.")
        email = _required_email(connection.get("expected_email"))
        if email in expected_emails:
            raise MailConfigError(
                "Mail profiles must use distinct expected identities."
            )
        expected_emails.add(email)

        adapters[alias] = str(provider)
        if provider == "gws":
            # Preserve the landed consumer layout so the atomic runtime cutover
            # does not move or duplicate provider-owned OAuth state.
            target = (config_root / alias).resolve()
            if target.parent != config_root:
                raise MailConfigError("Mail profile config directory is invalid.")
            account: MailAccountConfig = GwsAccountConfig(
                alias=alias,
                email=email,
                config_dir=target,
                binding_state=str(state),
            )
        else:
            target = (config_root / "proton" / f"{alias}.toml").resolve()
            if target.parent != (config_root / "proton").resolve():
                raise MailConfigError("Proton Bridge config path is invalid.")
            account = ProtonBridgeAccountConfig(
                alias=alias,
                email=email,
                local_config_path=target,
                binding_state=str(state),
            )
        if target in provider_paths:
            raise MailConfigError(
                "Mail profiles must use distinct provider config paths."
            )
        provider_paths.add(target)
        accounts[alias] = account

    if set(connections) != used_connections:
        raise MailConfigError("Project Index contains unused mail connections.")
    return accounts, adapters


def _load_project_routes(
    raw: dict[str, Any], *, profile_adapters: dict[str, str]
) -> dict[str, str]:
    projects = raw.get("projects")
    if not isinstance(projects, list):
        raise MailConfigError("Project Index projects must be a list.")
    routes: dict[str, str] = {}
    seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            raise MailConfigError("Project Index contains an invalid project entry.")
        key = project.get("key")
        if not isinstance(key, str) or not key.strip():
            raise MailConfigError("Project Index contains a project without a key.")
        key = key.strip()
        if key in seen:
            raise MailConfigError("Project Index contains duplicate project keys.")
        seen.add(key)
        profile = project.get("mail_profile")
        if profile is None:
            continue
        if not isinstance(profile, str) or profile not in profile_adapters:
            raise MailConfigError("Project references an unavailable mail profile.")
        routes[key] = profile
    return routes


def _required_alias(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 64
        or re.fullmatch(r"[a-z0-9][a-z0-9-]*", value.strip()) is None
    ):
        raise MailConfigError("Mail profile alias is invalid.")
    return value.strip()


def _required_email(value: object) -> str:
    if not isinstance(value, str):
        raise MailConfigError("Mail connection expected identity is missing.")
    email = value.strip()
    if len(email) > 254 or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) is None:
        raise MailConfigError("Mail connection expected identity is invalid.")
    return email
