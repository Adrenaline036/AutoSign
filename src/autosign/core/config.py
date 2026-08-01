from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AUTOSIGN_",
        extra="ignore",
    )

    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    master_key: SecretStr | None = None
    browser_session_timeout_seconds: int = 900
    browser_headless: bool = True
    auth_session_hours: int = 12
    auth_secure_cookie: bool = False
    auth_disabled: bool = False
    scheduler_poll_seconds: float = 15
    backup_enabled: bool = False
    backup_daily_time: str = "03:30"
    backup_timezone: str = "Asia/Shanghai"
    backup_retention_count: int = 7
    backup_poll_seconds: float = 60
    backup_password: SecretStr | None = None

    def prepare_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        database_path = (self.data_dir / "autosign.db").resolve().as_posix()
        return f"sqlite:///{database_path}"

    def require_master_key(self) -> str:
        if self.master_key is None or not self.master_key.get_secret_value():
            raise RuntimeError(
                "AUTOSIGN_MASTER_KEY is required. Run `python -m autosign init-key` once."
            )
        return self.master_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
