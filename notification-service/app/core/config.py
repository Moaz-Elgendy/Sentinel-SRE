"""
Application configuration.

All values are loaded from environment variables so nothing sensitive is
hardcoded. See .env at the repo root for the full list of variables.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database (this service owns its own database — one DB per
    # microservice, same Postgres pattern used in production).
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "notification_service"
    database_user: str = "app_user"
    database_password: str = "change_me"

    # Simulated delivery. No real email/SMS provider is wired up for this
    # demo — every notification is validated, persisted, and logged as if
    # sent. CHAOS_MODE (Phase 10) can force a configurable fraction of
    # sends to fail, to exercise Sentinel's incident detection.
    chaos_mode: bool = False
    chaos_failure_rate: float = 0.3

    # CORS: comma-separated list of allowed browser origins (not exercised
    # by anything yet — this service is only ever called server-to-server
    # today — kept configurable in case a future admin UI queries it
    # directly). See citizen-service/app/core/config.py for the same
    # pattern and rationale.
    cors_allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )


settings = Settings()
