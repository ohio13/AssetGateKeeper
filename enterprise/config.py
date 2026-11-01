"""Typed enterprise configuration.

One file, one shape, no surprises:

    [enterprise]
    session_secret_env = "AGK_SESSION_SECRET"

    [enterprise.database]
    dsn = ""                      # empty = local SQLite, exactly as before
    pool_min = 2
    pool_max = 10

    [enterprise.oidc]
    issuer = "https://studio.okta.com"
    client_id = "..."
    ...

Rules that this module enforces on purpose:

* **Secrets never come from the file.** Tokens, client keys and session keys are
  read from the environment only. A committed config cannot leak a credential
  because it has nowhere to put one; ``client_secret`` in a file is refused with
  an explanation rather than being quietly honoured.
* **Nothing is guessed.** An unknown key is an error, not a default: a typo in
  ``[enterprise.oidc]`` would otherwise silently disable authentication.
* **Deny by default.** RBAC starts closed; an empty rule set authorises nobody
  except the reserved ``director`` role.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:                                   # 3.11+
    import tomllib
except ModuleNotFoundError:            # pragma: no cover - 3.9/3.10
    try:
        import tomli as tomllib                        # type: ignore[no-redef]
    except ModuleNotFoundError:        # pragma: no cover
        tomllib = None                                 # type: ignore[assignment]

ENV_PREFIX = "AGK_"


class ConfigError(Exception):
    """A configuration problem, phrased for the person who has to fix it."""


#: Environment variable names, so a deployment can be described without a file.
ENV = {
    "database.dsn": "AGK_DATABASE_DSN",
    "oidc.issuer": "AGK_OIDC_ISSUER",
    "oidc.client_id": "AGK_OIDC_CLIENT_ID",
    "oidc.client_secret": "AGK_OIDC_CLIENT_SECRET",
    "oidc.redirect_uri": "AGK_OIDC_REDIRECT_URI",
    "saml.entity_id": "AGK_SAML_ENTITY_ID",
    "saml.sso_url": "AGK_SAML_SSO_URL",
    "saml.x509_cert": "AGK_SAML_CERT",
    "vcs.p4_port": "P4PORT",
    "vcs.p4_user": "P4USER",
    "session_secret": "AGK_SESSION_SECRET",
}

SECRET_KEYS = {"session_secret", "oidc.client_secret", "saml.signing_key"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _read(env_name: str, fallback: str = "") -> str:
    """Environment first, then the fallback (which is never a secret)."""
    return _env(env_name, "") or fallback


def _require_table(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table")
    return value


def _reject_unknown(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where} has unknown key(s): {', '.join(unknown)}; "
                          f"known keys: {', '.join(sorted(allowed))}")


def _as_int(table: dict, key: str, where: str, default: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be a whole number")
    return value


def _as_bool(table: dict, key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be true or false")
    return value


def _as_str(table: dict, key: str, where: str, default: str = "") -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string")
    return value.strip()


def _as_list(table: dict, key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a list, not a string")
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{where}.{key} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DatabaseConfig:
    """Where verdicts live. An empty DSN means SQLite, unchanged from 1.0."""

    dsn: str = ""
    sqlite_path: str = "asset_health.db"
    pool_min: int = 2
    pool_max: int = 10
    statement_timeout_ms: int = 30_000
    migrate_on_start: bool = True
    #: Read replicas, in preference order. Analytics and audit listings read
    #: here so a heavy dashboard query cannot slow down ingest.
    read_dsns: tuple[str, ...] = ()
    #: A failing endpoint trips after this many consecutive failures ...
    breaker_threshold: int = 5
    #: ... and is probed again after this many seconds.
    breaker_reset_seconds: float = 30.0
    #: How often the health loop pings each endpoint, in seconds (0 = off).
    health_interval_seconds: float = 30.0

    @property
    def is_postgres(self) -> bool:
        return self.dsn.lower().startswith(("postgres://", "postgresql://"))


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8080/enterprise/callback"
    scopes: tuple[str, ...] = ("openid", "profile", "email", "groups")
    groups_claim: str = "groups"
    username_claim: str = "email"
    allowed_domains: tuple[str, ...] = ()
    clock_skew_seconds: int = 120
    http_timeout: float = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id)


@dataclass(frozen=True)
class SAMLConfig:
    entity_id: str = ""
    sso_url: str = ""
    x509_cert: str = ""
    audience: str = ""
    groups_attribute: str = "groups"
    username_attribute: str = "mail"
    require_signature: bool = True
    allow_unsigned: bool = False          # explicit lab escape hatch, never default

    @property
    def enabled(self) -> bool:
        return bool(self.entity_id and self.sso_url)


@dataclass(frozen=True)
class VCSConfig:
    p4_port: str = ""
    p4_user: str = ""
    p4_client: str = ""
    p4_executable: str = "p4"
    p4_charset: str = ""
    lfs_command: str = "git"
    fetch_timeout: float = 60.0
    max_asset_bytes: int = 512 * 1024 * 1024

    @property
    def perforce_configured(self) -> bool:
        return bool(self.p4_port)


@dataclass(frozen=True)
class SessionConfig:
    secret: str = ""
    ttl_seconds: int = 12 * 3600
    cookie_name: str = "agk_session"
    secure_cookie: bool = True

    @property
    def enabled(self) -> bool:
        return len(self.secret) >= 32


@dataclass(frozen=True)
class GrantSpec:
    """One line of RBAC policy, as written in the config."""

    subject: str
    path: str
    permissions: tuple[str, ...]
    deny: bool = False


@dataclass(frozen=True)
class RBACConfig:
    grants: tuple[GrantSpec, ...] = ()
    default_deny: bool = True
    director_role: str = "director"

    @property
    def enabled(self) -> bool:
        return bool(self.grants)


@dataclass(frozen=True)
class AuditConfig:
    """SIEM forwarding. Off by default; a local JSONL file when enabled."""

    enabled: bool = False
    file: str = ""
    syslog_host: str = ""
    syslog_port: int = 514
    syslog_facility: int = 13
    syslog_transport: str = "udp"
    webhook_url: str = ""
    webhook_token: str = ""
    webhook_batch: int = 25
    webhook_interval: float = 2.0
    min_severity: str = "info"

    @property
    def forwarding(self) -> bool:
        return bool(self.syslog_host or self.webhook_url)


@dataclass(frozen=True)
class EnterpriseConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    oidc: OIDCConfig = field(default_factory=OIDCConfig)
    saml: SAMLConfig = field(default_factory=SAMLConfig)
    vcs: VCSConfig = field(default_factory=VCSConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    rbac: RBACConfig = field(default_factory=RBACConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    source: str = "defaults"
    warnings: tuple[str, ...] = ()

    @property
    def auth_enabled(self) -> bool:
        return self.oidc.enabled or self.saml.enabled


GRANT_KEYS = {"subject", "path", "permissions", "deny"}


def _load_grants(table: dict, warnings: list[str]) -> tuple[GrantSpec, ...]:
    raw = table.get("grant", [])
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ConfigError("[[enterprise.rbac.grant]] must be repeated tables")
    grants = []
    for index, item in enumerate(raw):
        where = f"[[enterprise.rbac.grant]] #{index + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a table")
        _reject_unknown(item, GRANT_KEYS, where)
        subject = _as_str(item, "subject", where)
        path = _as_str(item, "path", where) or "/"
        permissions = tuple(p.lower() for p in
                            (item.get("permissions") or []))
        if not subject:
            raise ConfigError(f"{where} needs a subject (user:name, group:name or role:name)")
        if not permissions:
            raise ConfigError(f"{where} has no permissions")
        grants.append(GrantSpec(subject=subject, path=path,
                                permissions=permissions,
                                deny=_as_bool(item, "deny", where, False)))
    return tuple(grants)


def load_config(path: str | Path | None = None) -> EnterpriseConfig:
    """Read the enterprise section, or return defaults.

    A missing file is not an error: a single-machine studio runs v1 exactly as
    before. A *present but broken* file is always an error.
    """
    warnings: list[str] = []
    document: dict = {}
    source = "defaults"

    if path is None:
        candidate = Path("enterprise.toml")
        if candidate.is_file():
            path = candidate
    if path is not None:
        file_path = Path(path)
        if not file_path.is_file():
            raise ConfigError(f"no such enterprise config: {file_path}")
        if tomllib is None:                       # pragma: no cover
            raise ConfigError("reading a config file needs Python 3.11+ or 'tomli'")
        with open(file_path, "rb") as handle:
            parsed = tomllib.load(handle)
        section = parsed.get("enterprise", parsed)
        document = _require_table(section, "[enterprise]")
        source = str(file_path)

    _reject_unknown(document, {"database", "oidc", "saml", "vcs", "session",
                               "rbac", "audit", "session_secret"}, "[enterprise]")

    # --- database ---
    db_table = _require_table(document.get("database"), "[enterprise.database]")
    _reject_unknown(db_table, {"dsn", "sqlite_path", "pool_min", "pool_max",
                               "statement_timeout_ms", "migrate_on_start",
                               "read_dsns", "breaker_threshold",
                               "breaker_reset_seconds", "health_interval_seconds"},
                    "[enterprise.database]")
    dsn = _read(ENV["database.dsn"], _as_str(db_table, "dsn", "[enterprise.database]"))
    database = DatabaseConfig(
        dsn=dsn,
        sqlite_path=_as_str(db_table, "sqlite_path", "[enterprise.database]",
                            "asset_health.db"),
        pool_min=_as_int(db_table, "pool_min", "[enterprise.database]", 2),
        pool_max=_as_int(db_table, "pool_max", "[enterprise.database]", 10),
        statement_timeout_ms=_as_int(db_table, "statement_timeout_ms",
                                     "[enterprise.database]", 30_000),
        migrate_on_start=_as_bool(db_table, "migrate_on_start",
                                  "[enterprise.database]", True),
        read_dsns=_as_list(db_table, "read_dsns", "[enterprise.database]"),
        breaker_threshold=_as_int(db_table, "breaker_threshold",
                                  "[enterprise.database]", 5),
        breaker_reset_seconds=float(_as_int(db_table, "breaker_reset_seconds",
                                            "[enterprise.database]", 30)),
        health_interval_seconds=float(_as_int(db_table, "health_interval_seconds",
                                              "[enterprise.database]", 30)),
    )
    if database.pool_min > database.pool_max:
        raise ConfigError("[enterprise.database] pool_min is larger than pool_max")

    # --- oidc ---
    oidc_table = _require_table(document.get("oidc"), "[enterprise.oidc]")
    _reject_unknown(oidc_table, {"issuer", "client_id", "client_secret",
                                 "redirect_uri", "scopes", "groups_claim",
                                 "username_claim", "allowed_domains",
                                 "clock_skew_seconds", "http_timeout"},
                    "[enterprise.oidc]")
    if "client_secret" in oidc_table:
        raise ConfigError(
            "refusing 'client_secret' in a config file: a committed secret is a "
            "leaked secret. Set AGK_OIDC_CLIENT_SECRET in the environment instead.")
    oidc = OIDCConfig(
        issuer=_read(ENV["oidc.issuer"], _as_str(oidc_table, "issuer", "[enterprise.oidc]")),
        client_id=_read(ENV["oidc.client_id"],
                        _as_str(oidc_table, "client_id", "[enterprise.oidc]")),
        client_secret=_env(ENV["oidc.client_secret"]),
        redirect_uri=_read(ENV["oidc.redirect_uri"],
                           _as_str(oidc_table, "redirect_uri", "[enterprise.oidc]",
                                   OIDCConfig().redirect_uri)),
        scopes=_as_list(oidc_table, "scopes", "[enterprise.oidc]") or OIDCConfig().scopes,
        groups_claim=_as_str(oidc_table, "groups_claim", "[enterprise.oidc]", "groups"),
        username_claim=_as_str(oidc_table, "username_claim", "[enterprise.oidc]", "email"),
        allowed_domains=tuple(d.lower().lstrip("@") for d in
                              _as_list(oidc_table, "allowed_domains", "[enterprise.oidc]")),
        clock_skew_seconds=_as_int(oidc_table, "clock_skew_seconds",
                                   "[enterprise.oidc]", 120),
        http_timeout=float(_as_int(oidc_table, "http_timeout", "[enterprise.oidc]", 10)),
    )
    if oidc.enabled and not oidc.client_secret:
        warnings.append("OIDC is configured without AGK_OIDC_CLIENT_SECRET: "
                        "only public (PKCE) clients will work")

    # --- saml ---
    saml_table = _require_table(document.get("saml"), "[enterprise.saml]")
    _reject_unknown(saml_table, {"entity_id", "sso_url", "x509_cert", "audience",
                                 "groups_attribute", "username_attribute",
                                 "require_signature", "allow_unsigned"},
                    "[enterprise.saml]")
    saml = SAMLConfig(
        entity_id=_read(ENV["saml.entity_id"],
                        _as_str(saml_table, "entity_id", "[enterprise.saml]")),
        sso_url=_read(ENV["saml.sso_url"], _as_str(saml_table, "sso_url", "[enterprise.saml]")),
        x509_cert=_read(ENV["saml.x509_cert"],
                        _as_str(saml_table, "x509_cert", "[enterprise.saml]")),
        audience=_as_str(saml_table, "audience", "[enterprise.saml]"),
        groups_attribute=_as_str(saml_table, "groups_attribute", "[enterprise.saml]", "groups"),
        username_attribute=_as_str(saml_table, "username_attribute", "[enterprise.saml]", "mail"),
        require_signature=_as_bool(saml_table, "require_signature", "[enterprise.saml]", True),
        allow_unsigned=_as_bool(saml_table, "allow_unsigned", "[enterprise.saml]", False),
    )
    if saml.enabled and not saml.x509_cert and saml.require_signature:
        warnings.append("SAML has no x509_cert: signature verification will fail closed")

    # --- vcs ---
    vcs_table = _require_table(document.get("vcs"), "[enterprise.vcs]")
    _reject_unknown(vcs_table, {"p4_port", "p4_user", "p4_client", "p4_executable",
                                "p4_charset", "lfs_command", "fetch_timeout",
                                "max_asset_bytes"},
                    "[enterprise.vcs]")
    vcs = VCSConfig(
        p4_port=_read(ENV["vcs.p4_port"],
                      _as_str(vcs_table, "p4_port", "[enterprise.vcs]")),
        p4_user=_read(ENV["vcs.p4_user"],
                      _as_str(vcs_table, "p4_user", "[enterprise.vcs]")),
        p4_client=_as_str(vcs_table, "p4_client", "[enterprise.vcs]"),
        p4_executable=_as_str(vcs_table, "p4_executable", "[enterprise.vcs]", "p4"),
        p4_charset=_as_str(vcs_table, "p4_charset", "[enterprise.vcs]"),
        lfs_command=_as_str(vcs_table, "lfs_command", "[enterprise.vcs]", "git"),
        fetch_timeout=float(_as_int(vcs_table, "fetch_timeout", "[enterprise.vcs]", 60)),
        max_asset_bytes=_as_int(vcs_table, "max_asset_bytes", "[enterprise.vcs]",
                                512 * 1024 * 1024),
    )

    # --- session ---
    session_table = _require_table(document.get("session"), "[enterprise.session]")
    _reject_unknown(session_table, {"ttl_seconds", "cookie_name", "secure_cookie"},
                    "[enterprise.session]")
    secret = _read(ENV["session_secret"],
                   _as_str(document, "session_secret", "[enterprise]", ""))
    if secret and len(secret) < 32:
        raise ConfigError("the session secret must be at least 32 characters")
    session = SessionConfig(
        secret=secret,
        ttl_seconds=_as_int(session_table, "ttl_seconds", "[enterprise.session]", 43200),
        cookie_name=_as_str(session_table, "cookie_name", "[enterprise.session]",
                            "agk_session"),
        secure_cookie=_as_bool(session_table, "secure_cookie", "[enterprise.session]", True),
    )

    # --- audit ---
    audit_table = _require_table(document.get("audit"), "[enterprise.audit]")
    _reject_unknown(audit_table, {"enabled", "file", "syslog_host", "syslog_port",
                                  "syslog_facility", "syslog_transport",
                                  "webhook_url", "webhook_batch",
                                  "webhook_interval", "min_severity"},
                    "[enterprise.audit]")
    if "webhook_token" in audit_table:
        raise ConfigError(
            "refusing 'webhook_token' in a config file: set "
            "AGK_AUDIT_WEBHOOK_TOKEN in the environment instead")
    audit = AuditConfig(
        enabled=_as_bool(audit_table, "enabled", "[enterprise.audit]", False),
        file=_as_str(audit_table, "file", "[enterprise.audit]"),
        syslog_host=_as_str(audit_table, "syslog_host", "[enterprise.audit]"),
        syslog_port=_as_int(audit_table, "syslog_port", "[enterprise.audit]", 514),
        syslog_facility=_as_int(audit_table, "syslog_facility", "[enterprise.audit]", 13),
        syslog_transport=_as_str(audit_table, "syslog_transport",
                                 "[enterprise.audit]", "udp"),
        webhook_url=_as_str(audit_table, "webhook_url", "[enterprise.audit]"),
        webhook_token=_env("AGK_AUDIT_WEBHOOK_TOKEN"),
        webhook_batch=_as_int(audit_table, "webhook_batch", "[enterprise.audit]", 25),
        webhook_interval=float(_as_int(audit_table, "webhook_interval",
                                       "[enterprise.audit]", 2)),
        min_severity=_as_str(audit_table, "min_severity", "[enterprise.audit]", "info"),
    )
    if audit.syslog_transport not in ("udp", "tcp"):
        raise ConfigError("[enterprise.audit] syslog_transport must be udp or tcp")
    if audit.enabled and not (audit.file or audit.forwarding):
        warnings.append("audit is enabled with no destination: a local "
                        "agk-audit.jsonl will be written")

    # --- rbac ---
    rbac_table = _require_table(document.get("rbac"), "[enterprise.rbac]")
    _reject_unknown(rbac_table, {"grant", "default_deny", "director_role"},
                    "[enterprise.rbac]")
    rbac = RBACConfig(
        grants=_load_grants(rbac_table, warnings),
        default_deny=_as_bool(rbac_table, "default_deny", "[enterprise.rbac]", True),
        director_role=_as_str(rbac_table, "director_role", "[enterprise.rbac]", "director"),
    )

    return EnterpriseConfig(database=database, oidc=oidc, saml=saml, vcs=vcs,
                            session=session, rbac=rbac, audit=audit, source=source,
                            warnings=tuple(warnings))


def describe(config: EnterpriseConfig) -> list[str]:
    """A short, secret-free summary for ``agk-enterprise doctor``."""
    return [
        f"config        {config.source}",
        f"database      {'PostgreSQL' if config.database.is_postgres else 'SQLite'} "
        f"({'dsn set' if config.database.dsn else config.database.sqlite_path})",
        f"oidc          {'on: ' + config.oidc.issuer if config.oidc.enabled else 'off'}",
        f"saml          {'on: ' + config.saml.entity_id if config.saml.enabled else 'off'}",
        f"sso policy    "
        f"{'signed assertions required' if config.saml.require_signature else 'UNSIGNED ALLOWED'}",
        f"perforce      {config.vcs.p4_port or 'not configured'}",
        f"rbac          {len(config.rbac.grants)} grant(s), "
        f"{'deny by default' if config.rbac.default_deny else 'ALLOW BY DEFAULT'}",
        f"session       {'on' if config.session.enabled else 'off (no secret)'}",
        f"read replicas {len(config.database.read_dsns)} "
        f"({', '.join(config.database.read_dsns)})" if config.database.read_dsns
        else "read replicas none",
        f"audit         {'on: ' + (', '.join(filter(None, [config.audit.file, config.audit.syslog_host, config.audit.webhook_url]))) if config.audit.enabled else 'off'}",
    ]
