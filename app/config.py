from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 480
    # Stored as a plain comma-separated string so pydantic-settings never
    # tries to JSON-parse it. Use the cors_origins property everywhere.
    cors_origins_raw: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")

    # Cookie settings — in production (HTTPS cross-origin) set both to True/"none" via env vars.
    # For local dev keep the defaults (False / "lax").
    cookie_secure: bool = Field(default=False)
    cookie_samesite: str = Field(default="lax")

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]
    gold_api_key: str = ""
    gold_api_url: str = "https://www.goldapi.io/api/XAU/USD"
    gold_refresh_minutes: int = 15
    seed_admin_email: str = ""
    seed_admin_password: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_public_url: str = ""
    discord_webhook_url: str = ""
    discord_alert_user_id: str = ""
    gold_alert_failure_threshold: int = 3


settings = Settings()
