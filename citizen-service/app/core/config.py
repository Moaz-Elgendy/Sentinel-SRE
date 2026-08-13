"""
Application configuration.

All values are loaded from environment variables so nothing sensitive is
hardcoded. See .env.example at the repo root for the full list of variables.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "citizen_portal"
    database_user: str = "app_user"
    database_password: str = "change_me"

    # Auth
    jwt_secret: str = "change_me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24h

    # Service-to-service
    notification_service_url: str = "http://notification-service:8000"

    # CORS: comma-separated list of allowed browser origins. Defaults cover
    # the two ways "localhost" gets typed/opened locally — browsers treat
    # them as different origins, so both must be listed explicitly or one
    # of them gets silently blocked.
    cors_allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    # Chaos engineering (Phase 10 — disabled by default)
    chaos_mode: bool = False

    # File uploads
    upload_dir: str = "/data/uploads"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )


settings = Settings()
