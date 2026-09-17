"""Configuration from environment variables plus an optional TOML file.

Everything a run needs comes from the environment (see ``.env.example``);
``config.toml`` only adds event-series label mappings, which are too structured
for environment variables. Secrets never live in the repository.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from eventor_client import AU_BASE_URL
from eventor_client.models import Event

# Explicit "switch this off" value. An empty variable means "use the default", because
# GitHub Actions passes unset repository variables through as empty strings.
DISABLED = "none"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SeriesRule:
    """Map events to a series label by ID and/or by name pattern."""

    name: str
    label: str
    event_ids: frozenset[int] = frozenset()
    name_patterns: tuple[re.Pattern[str], ...] = ()

    def matches(self, event: Event) -> bool:
        if event.id in self.event_ids or event.parent_event_id in self.event_ids:
            return True
        return any(p.search(event.name) for p in self.name_patterns)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    eventor_base_url: str
    eventor_api_key: str
    google_user: str
    google_service_account: str | None = None
    google_service_account_file: Path | None = None
    eventor_cache_dir: Path | None = None
    eventor_cache_ttl_seconds: float = 3600.0
    window_months: int = 12
    previous_year_until_month: int = 3
    require_paid: bool = True
    label_all: str = "Eventor"
    label_member: str = "Member"
    label_entrant: str = "Entrant"
    update_names: bool = True
    sync_addresses: bool = True
    adopt_existing: bool = True
    max_removal_fraction: float = 0.25
    phone_country_code: str | None = None
    series: tuple[SeriesRule, ...] = ()
    report_path: Path = Path("report.json")
    config_path: Path | None = None

    @property
    def is_australian_instance(self) -> bool:
        host = urlparse(self.eventor_base_url).hostname or ""
        return host.endswith("orienteering.asn.au")


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _str(env: Mapping[str, str], name: str, default: str) -> str:
    return (env.get(name) or "").strip() or default


def default_phone_country_code(base_url: str) -> str | None:
    """Country calling code implied by the Eventor instance, for E.164 formatting."""
    host = urlparse(base_url).hostname or ""
    if host.endswith("orienteering.asn.au"):
        return "61"
    if host.endswith("orientering.se"):
        return "46"
    if host.endswith("orientering.no"):
        return "47"
    return None


def load_series(path: Path) -> tuple[SeriesRule, ...]:
    """Read ``[series.<name>]`` tables from a TOML file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    rules: list[SeriesRule] = []
    for name, table in (data.get("series") or {}).items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [series.{name}] must be a table")
        label = str(table.get("label") or "").strip()
        if not label:
            raise ConfigError(f"{path}: [series.{name}] needs a label")
        ids = table.get("event_ids") or []
        patterns = table.get("name_patterns") or []
        try:
            event_ids = frozenset(int(i) for i in ids)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: [series.{name}].event_ids must be integers") from exc
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(str(pattern), re.IGNORECASE))
            except re.error as exc:
                raise ConfigError(
                    f"{path}: [series.{name}] bad name pattern {pattern!r}: {exc}"
                ) from exc
        if not event_ids and not compiled:
            raise ConfigError(f"{path}: [series.{name}] needs event_ids or name_patterns")
        rules.append(
            SeriesRule(name=name, label=label, event_ids=event_ids, name_patterns=tuple(compiled))
        )
    return tuple(rules)


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    config_path: Path | None = None,
    require_google: bool = True,
) -> SyncConfig:
    """Build a :class:`SyncConfig` from ``env`` (default: ``os.environ``).

    ``config_path`` overrides ``SYNC_CONFIG``; when neither is set and a
    ``config.toml`` exists in the working directory it is used.
    """
    env = os.environ if env is None else env
    missing: list[str] = []

    eventor_key = (env.get("EVENTOR_API_KEY") or "").strip()
    if not eventor_key:
        missing.append("EVENTOR_API_KEY")
    google_user = (env.get("GOOGLE_USER") or "").strip()
    service_account = (env.get("GOOGLE_SERVICE_ACCOUNT") or "").strip() or None
    key_file_raw = (env.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if require_google:
        if not google_user:
            missing.append("GOOGLE_USER")
        if not service_account and not key_file_raw:
            missing.append("GOOGLE_SERVICE_ACCOUNT (or GOOGLE_SERVICE_ACCOUNT_FILE)")
    if missing:
        raise ConfigError("missing required configuration: " + ", ".join(missing))
    if google_user and "@" not in google_user:
        raise ConfigError(f"GOOGLE_USER must be an email address, got {google_user!r}")

    base_url = (env.get("EVENTOR_BASE_URL") or AU_BASE_URL).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigError(f"EVENTOR_BASE_URL must be an http(s) URL, got {base_url!r}")

    chosen_config = config_path
    if chosen_config is None:
        raw = (env.get("SYNC_CONFIG") or "").strip()
        if raw:
            chosen_config = Path(raw)
        elif Path("config.toml").is_file():
            chosen_config = Path("config.toml")
    series: tuple[SeriesRule, ...] = ()
    if chosen_config is not None:
        if not chosen_config.is_file():
            raise ConfigError(f"config file not found: {chosen_config}")
        series = load_series(chosen_config)

    country_raw = (env.get("SYNC_PHONE_COUNTRY_CODE") or "").strip().lstrip("+")
    if not country_raw:
        phone_country_code: str | None = default_phone_country_code(base_url)
    elif country_raw.lower() == DISABLED:
        phone_country_code = None
    else:
        if not country_raw.isdigit():
            raise ConfigError(
                f"SYNC_PHONE_COUNTRY_CODE must be digits or 'none', got {country_raw!r}"
            )
        phone_country_code = country_raw

    previous_until = _int(env, "SYNC_PREVIOUS_YEAR_UNTIL_MONTH", 3)
    if previous_until > 12:
        raise ConfigError("SYNC_PREVIOUS_YEAR_UNTIL_MONTH must be between 0 and 12")
    removal_percent = _int(env, "SYNC_MAX_REMOVAL_PERCENT", 25)
    if removal_percent > 100:
        raise ConfigError("SYNC_MAX_REMOVAL_PERCENT must be between 0 and 100")

    cache_dir_raw = (env.get("EVENTOR_CACHE_DIR") or "").strip()
    return SyncConfig(
        eventor_base_url=base_url,
        eventor_api_key=eventor_key,
        google_user=google_user,
        google_service_account=service_account,
        google_service_account_file=Path(key_file_raw) if key_file_raw else None,
        eventor_cache_dir=Path(cache_dir_raw) if cache_dir_raw else None,
        eventor_cache_ttl_seconds=float(_int(env, "EVENTOR_CACHE_TTL_SECONDS", 3600)),
        window_months=_int(env, "SYNC_WINDOW_MONTHS", 12, minimum=1),
        previous_year_until_month=previous_until,
        require_paid=_bool(env.get("SYNC_REQUIRE_PAID"), True),
        label_all=_str(env, "SYNC_LABEL_ALL", "Eventor"),
        label_member=_str(env, "SYNC_LABEL_MEMBER", "Member"),
        label_entrant=_str(env, "SYNC_LABEL_ENTRANT", "Entrant"),
        update_names=_bool(env.get("SYNC_UPDATE_NAMES"), True),
        sync_addresses=_bool(env.get("SYNC_ADDRESSES"), True),
        adopt_existing=_bool(env.get("SYNC_ADOPT_EXISTING"), True),
        max_removal_fraction=removal_percent / 100,
        phone_country_code=phone_country_code,
        series=series,
        report_path=Path((env.get("SYNC_REPORT_PATH") or "report.json").strip()),
        config_path=chosen_config,
    )
