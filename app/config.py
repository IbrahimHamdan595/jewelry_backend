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
    seed_admin_email: str = "owner@maisonzahab.com"
    seed_admin_password: str = "ChangeMe123!"
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""


settings = Settings()
