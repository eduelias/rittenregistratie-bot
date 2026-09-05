"""Configuration loading from environment and YAML."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment / .env."""

    model_config = SettingsConfigDict(
        env_prefix="RIT_", env_file=".env", extra="ignore"
    )

    # WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = "changeme"
    whatsapp_graph_version: str = "v25.0"  # Meta Graph API version
    allowed_sender: str = ""  # E.164 without '+', e.g. 31612345678
    # Admin numbers (comma-separated, E.164 without '+') who can approve joins.
    admin_numbers: str = ""
    # Allow unregistered numbers to request onboarding (else silently ignored).
    onboarding_enabled: bool = True
    # Maximum number of registered users/cars allowed.
    max_users: int = 5
    # How to acknowledge a logged trip: "text" (summary message), "reaction"
    # (thumbs-up on your message; text only when the bot needs something or has
    # extra info) or "both".
    reply_mode: str = "text"

    # Trajectory
    google_maps_api_key: str = ""
    # Look up the driven route when a trip's distance is not the usual one, and
    # mention it in the reply. Off by default: the ledger has no field for a
    # route, so anything found here is reported once and then lost. Deciding
    # what the Route column should say is a job for export time, where the
    # whole year is in hand (see docs/plugins.md).
    route_on_deviation: bool = False

    # Plugin selection (entry-point names)
    odometer_source: str = "whatsapp_manual"
    trajectory_provider: str = "maps_link"
    private_cap_plugin: str = "warn"
    # Optional per-user override: numbers in private_cap_override_numbers use
    # private_cap_plugin_override instead of the default cap plugin.
    private_cap_plugin_override: str = ""
    private_cap_override_numbers: str = ""

    # Event plugins (entry-point group "rittenregistratie.events"): "*" loads
    # every installed plugin, "" loads none, or a comma-separated list of names.
    event_plugins: str = "*"

    # Vehicle-telemetry hooks (POST /hooks/<plugin>/<car_id>): shared secret
    # sent as X-Hook-Secret. Empty disables the endpoint.
    hook_secret: str = ""
    # How close (metres) a trip's end position must be to a known location's
    # coordinates to count as that place (per-location radius_m overrides).
    place_radius_m: int = 300

    # Compliance
    private_cap_km: int = 500

    # First-trip seed
    seed_address: str = "Home"
    seed_odometer: int = 0

    # Paths
    data_dir: Path = Path("data")
    config_dir: Path = Path("config")

    @property
    def locations_file(self) -> Path:
        return self.config_dir / "locations.yaml"

    @property
    def routes_file(self) -> Path:
        return self.config_dir / "routes.yaml"

    @property
    def learned_locations_file(self) -> Path:
        """Addresses learned from chat replies. Kept out of the committed config."""
        return self.data_dir / "locations-learned.yaml"

    @property
    def exports_dir(self) -> Path:
        """Where on-demand spreadsheets are written."""
        return self.data_dir / "exports"

    def event_plugin_selection(self) -> Optional[list[str]]:
        """None = load all installed event plugins; a list = only those names."""
        raw = (self.event_plugins or "").strip()
        if raw == "*":
            return None
        return [n.strip() for n in raw.split(",") if n.strip()]

    @property
    def cars_file(self) -> Path:
        return self.config_dir / "cars.yaml"

    def state_file(self, car_id: str) -> Path:
        return self.data_dir / f"state-{car_id}.json"

    def raw_ledger_file(self, car_id: str) -> Path:
        return self.data_dir / f"raw-ledger-{car_id}.jsonl"

    @property
    def onboarding_file(self) -> Path:
        return self.data_dir / "onboarding.json"

    @property
    def active_car_file(self) -> Path:
        """Per-phone active car, for phones that drive several cars."""
        return self.data_dir / "active-car.json"

    def admin_list(self) -> list[str]:
        import re
        return [re.sub(r"\D", "", n) for n in self.admin_numbers.split(",") if n.strip()]

    def cap_override_list(self) -> list[str]:
        import re
        return [
            re.sub(r"\D", "", n)
            for n in self.private_cap_override_numbers.split(",")
            if n.strip()
        ]


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def save_location(path: Path, name: str, address: str, **extra) -> None:
    """Add or update a known location in a locations.yaml file.

    The name is stored normalized (lower-case, single spaces) so lookups are
    case-insensitive and one place never gets two entries. ``extra`` may carry
    ``lat``/``lon``/``radius_m``/``private`` for vehicle-telemetry matching.

    An existing entry is updated field by field, not replaced. A place is
    taught in more than one way — coordinates arrive from the car, the address
    from a chat reply, ``private`` from the driver — and each of those calls
    knows only its own part. Replacing the entry meant whichever came last
    silently dropped the rest: answering an address prompt for a place the car
    had already located wiped its coordinates and its private flag, so the
    next trip there was neither recognised nor classified.
    """
    from .routes import normalize_name
    data = load_yaml(path)
    key = normalize_name(name)
    entry = dict(data.get(key) or {})
    entry["address"] = address
    entry.update({k: v for k, v in extra.items() if v is not None})
    data[key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=True, allow_unicode=True)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
    return _settings
