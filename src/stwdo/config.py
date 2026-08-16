"""Configuration loading: config.yaml + selectors.yaml + profile.yaml + .env.

Everything opens files with an explicit utf-8 encoding — Windows still defaults
to cp1252 and every one of these files carries German text (ü, ², €).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError


class ConfigError(RuntimeError):
    """Raised when configuration is missing or structurally invalid."""


# --------------------------------------------------------------------------- #
# config.yaml
# --------------------------------------------------------------------------- #


class SiteConfig(BaseModel):
    base_url: str = "https://www.stwdo.de"
    offers_path: str = "/wohnen/aktuelle-wohnangebote"
    offer_detail_path: str = "/freie-zimmer/{offer_id}"
    language: str = "de"
    timezone: str = "Europe/Berlin"

    def offers_url(self) -> str:
        return self.base_url.rstrip("/") + self.offers_path

    def detail_url(self, offer_id: str) -> str:
        return self.base_url.rstrip("/") + self.offer_detail_path.format(offer_id=offer_id)


class BrowserConfig(BaseModel):
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    locale: str = "de-DE"
    gate_timeout_seconds: int = 45


class BurstConfig(BaseModel):
    enabled: bool = True
    weekdays: list[str] = Field(default_factory=lambda: ["mon", "wed"])
    start: str = "09:55"
    end: str = "10:45"
    interval_seconds: int = 15


class QuietHoursConfig(BaseModel):
    enabled: bool = False
    start: str = "02:00"
    end: str = "06:00"
    interval_seconds: int = 600


class PollingConfig(BaseModel):
    baseline_interval_seconds: int = 120
    jitter_ratio: float = 0.25
    burst: BurstConfig = Field(default_factory=BurstConfig)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    failure_alert_threshold: int = 3
    backoff_max_seconds: int = 900


class WeightsConfig(BaseModel):
    rent: float = 0.35
    room_type: float = 0.25
    size: float = 0.20
    location: float = 0.15
    availability: float = 0.05

    def total(self) -> float:
        return self.rent + self.room_type + self.size + self.location + self.availability


class RentScoring(BaseModel):
    floor_rent: float = 280.0


class SizeScoring(BaseModel):
    ideal_size: float = 28.0


class AvailabilityScoring(BaseModel):
    saturation_count: int = 10


class LocationScoring(BaseModel):
    campus_anchor: str = ""
    good_minutes: float = 10.0
    bad_minutes: float = 45.0
    default_minutes: float = 30.0
    # Per-address travel time, most specific.
    walking_minutes: dict[str, float] = Field(default_factory=dict)
    # Per-city fallback for addresses not in the table above — this is what makes
    # "prefer Dortmund" hold for buildings that have not been listed before.
    city_minutes: dict[str, float] = Field(default_factory=dict)


class RulesConfig(BaseModel):
    cities: list[str] = Field(default_factory=lambda: ["Dortmund"])
    max_rent: float = 450.0
    min_size: float = 15.0
    allowed_types: list[str] = Field(
        default_factory=lambda: ["single_apartment", "shared_2", "shared_3", "shared_4"]
    )
    auto_apply_min_score: float = 70.0
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    rent_scoring: RentScoring = Field(default_factory=RentScoring)
    size_scoring: SizeScoring = Field(default_factory=SizeScoring)
    type_preference: list[str] = Field(
        default_factory=lambda: ["single_apartment", "shared_2", "shared_3", "shared_4"]
    )
    availability_scoring: AvailabilityScoring = Field(default_factory=AvailabilityScoring)
    location_scoring: LocationScoring = Field(default_factory=LocationScoring)


class TelegramConfig(BaseModel):
    enabled: bool = True
    send_screenshots: bool = True


class NotificationsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class ApplicationConfig(BaseModel):
    live_enabled: bool = False
    document_reminder: str = ""


class LLMConfig(BaseModel):
    enabled: bool = False
    model: str = "claude-sonnet-5"


class StorageConfig(BaseModel):
    database_path: str = "data/stwdo.sqlite3"
    storage_state_path: str = "data/storage_state.json"
    evidence_dir: str = "data/evidence"
    log_path: str = "data/watchdog.log"


class AppConfig(BaseModel):
    site: SiteConfig = Field(default_factory=SiteConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Filled by load_config(); not part of the YAML.
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def path(self, relative: str) -> Path:
        """Resolve a storage path against the project root, creating parents."""
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


# --------------------------------------------------------------------------- #
# profile.yaml
# --------------------------------------------------------------------------- #


class PersonalProfile(BaseModel):
    """Mirrors the Wohnungshelden form STWDO embeds on each offer page.

    `salutation` and `nationality` must match a dropdown option exactly — see
    profile.example.yaml for the allowed values.
    """

    salutation: str = ""
    first_name: str = ""
    last_name: str = ""
    # Fallbacks, used only when `contacts` below is empty.
    email: str = ""
    phone: str = ""
    mobile: str = ""          # required, format: +49 1579 123456
    date_of_birth: str = ""   # ISO in this file; converted to DD.MM.YYYY on fill
    nationality: str = ""


class StudyProfile(BaseModel):
    university: str = ""       # must match a dropdown option exactly
    start_semester: str = ""   # "Sommersemester (Summer semester)" | "Wintersemester (Winter semester)"
    start_year: str = ""
    semesters_wanted: str = "" # "For how many semesters would you like to rent the room?"


class HousingProfile(BaseModel):
    max_total_rent: str = ""       # the form asks for this explicitly
    wheelchair_required: bool = False
    notes: str = ""


class ConsentProfile(BaseModel):
    """Both boxes are part of the application and must be affirmed to submit."""

    confirm_enrollment_certificate: bool = False
    confirm_privacy_policy: bool = False


class ContactPair(BaseModel):
    """One email plus the mobile number that goes with it.

    Pairs are used in rotation: attempt 1 uses the first pair, attempt 2 the
    second, and so on. An "attempt" only advances when a submission is actually
    started, and the application lock permits exactly one of those until a human
    explicitly resets it — so rotation can never produce two live applications.
    """

    email: str = ""
    mobile: str = ""


class Profile(BaseModel):
    personal: PersonalProfile = Field(default_factory=PersonalProfile)
    study: StudyProfile = Field(default_factory=StudyProfile)
    housing: HousingProfile = Field(default_factory=HousingProfile)
    consents: ConsentProfile = Field(default_factory=ConsentProfile)
    contacts: list[ContactPair] = Field(default_factory=list)

    def contact_for_attempt(self, attempt_index: int) -> Optional[ContactPair]:
        """The pair to use for a given zero-based attempt, wrapping at the end."""
        usable = [c for c in self.contacts if c.email or c.mobile]
        if not usable:
            return None
        return usable[attempt_index % len(usable)]

    def flat(self) -> dict[str, str]:
        """Flatten to the `key -> value` map that the form filler consumes.

        Keys here are what `selectors.yaml -> application_form.fields` maps from.
        Booleans become "true"/"false" so a single string map covers every field
        type.
        """
        merged: dict[str, str] = {}
        # `contacts` is handled separately by contact_for_attempt(); the section
        # loop below covers the flat, single-valued fields.
        for section in (self.personal, self.study, self.housing, self.consents):
            for key, value in section.model_dump().items():
                if isinstance(value, bool):
                    merged[key] = "true" if value else "false"
                else:
                    merged[key] = "" if value is None else str(value)
        return merged


# --------------------------------------------------------------------------- #
# secrets (.env)
# --------------------------------------------------------------------------- #


class Secrets(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    anthropic_api_key: Optional[str] = None


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing configuration file: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def find_root(start: Optional[Path] = None) -> Path:
    """Walk up from `start` looking for config.yaml. Falls back to cwd."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "config.yaml").is_file():
            return candidate
    return current


def load_config(root: Optional[Path] = None) -> AppConfig:
    project_root = find_root(root)
    data = _read_yaml(project_root / "config.yaml")
    try:
        config = AppConfig(**data)
    except ValidationError as exc:
        raise ConfigError(f"config.yaml failed validation:\n{exc}") from exc

    total = config.rules.weights.total()
    if abs(total - 1.0) > 0.001:
        raise ConfigError(
            f"rules.weights must sum to 1.0, got {total:.3f}. "
            "Adjust config.yaml so scores stay on a 0-100 scale."
        )

    config.root = project_root
    return config


def load_selectors(root: Optional[Path] = None) -> dict[str, Any]:
    project_root = find_root(root)
    return _read_yaml(project_root / "selectors.yaml")


def load_profile(root: Optional[Path] = None) -> Profile:
    project_root = find_root(root)
    path = project_root / "profile.yaml"
    if not path.is_file():
        raise ConfigError(
            "profile.yaml not found. Copy profile.example.yaml to profile.yaml "
            "and fill in your details (it is gitignored)."
        )
    try:
        return Profile(**_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"profile.yaml failed validation:\n{exc}") from exc


def load_secrets(root: Optional[Path] = None) -> Secrets:
    project_root = find_root(root)
    env_path = project_root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, encoding="utf-8")
    return Secrets(
        telegram_bot_token=os.getenv("STWDO_TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("STWDO_TELEGRAM_CHAT_ID") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )
