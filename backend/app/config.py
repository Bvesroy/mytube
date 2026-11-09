"""Configuration. Every knob is an environment variable with a safe default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def default_data_dir() -> Path:
    override = os.environ.get("MYTUBE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "mytube"
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return Path.home() / "Library" / "Application Support" / "mytube"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mytube"


class Settings(BaseSettings):
    """Process configuration.

    Local-first defaults: loopback bind, no token required, no telemetry, no outbound
    calls except the metadata adapter the user invokes.
    """

    model_config = SettingsConfigDict(env_prefix="MYTUBE_", env_file=".env", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8010
    api_token: str | None = None
    log_level: str = "INFO"

    per_channel: int = 50
    sync_workers: int = 4
    sync_timeout_s: int = 300
    job_poll_s: float = 0.5
    job_stale_s: int = 120
    worker_heartbeat_s: float = 2.0

    # Project paths (the Takeout app this backend serves)
    project_root: Path = Path(__file__).resolve().parents[2]
    images_enabled: bool = False

    @field_validator("host")
    @classmethod
    def _host_not_bare_all(cls, value: str) -> str:
        return value.strip()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mytube.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def requires_auth(self) -> bool:
        return self.host not in LOOPBACK_HOSTS

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.cache_dir, self.backup_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class Runtime:
    """Everything a service needs, injected rather than imported as globals."""

    settings: Settings


def settings_for(data_dir: Path, **overrides) -> Settings:
    """Test helper: an isolated instance that never reads the user's env file."""
    return Settings(data_dir=data_dir, **overrides)
