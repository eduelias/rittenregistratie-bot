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
    allowed_sender: str = ""  # E.164 without '+', e.g. 31612345678

    # Trajectory
    google_maps_api_key: str = ""

    # Plugin selection (entry-point names)
    odometer_source: str = "whatsapp_manual"
    trajectory_provider: str = "maps_link"
    delta_allocator: str = "noop"
    private_cap_plugin: str = "warn"

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
    def cars_file(self) -> Path:
        return self.config_dir / "cars.yaml"

    def state_file(self, car_id: str) -> Path:
        return self.data_dir / f"state-{car_id}.json"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.data_dir.mkdir(parents=True, exist_ok=True)
    return _settings
