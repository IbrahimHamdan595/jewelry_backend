from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# The only ENVIRONMENT values that count as "not production". Everything else —
# unset, "production", a typo like "prod" — is production (NEX-47).
_LOCAL_ENVIRONMENTS = {"development", "dev", "local", "test"}


def _pem(value: str) -> str:
    """A PEM as pasted into an env-var UI: often on one line, with newlines
    written as the two characters backslash-n, sometimes still in quotes."""
    return value.strip().strip("\"'").replace("\\n", "\n").strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Deliberately defaults to production: a deploy that forgets the variable
    # gets the locked-down behaviour. Set ENVIRONMENT=development locally.
    environment: str = "production"

    database_url: str
    jwt_secret: str
    # The SHARED-SECRET algorithm only (HS256/HS384/HS512). RS256 is not
    # selected here: it is switched on by JWT_PRIVATE_KEY below.
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 480
    # NEX-54: asymmetric signing, so the frontend can verify a session without
    # holding anything that can mint one. PEM; literal "\n" for newlines is fine.
    #   JWT_PRIVATE_KEY set  → new tokens are signed RS256 with it.
    #   JWT_PRIVATE_KEY unset → HS256 with JWT_SECRET, exactly as before.
    #   JWT_PUBLIC_KEY       → verifies RS256 tokens (derived from the private
    #                          key when left unset). This is the half Vercel gets.
    #   JWT_ACCEPT_HS256     → keep accepting shared-secret tokens. True for the
    #                          migration window; set false once every HS256
    #                          session has expired (JWT_EXPIRES_MINUTES later).
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_accept_hs256: bool = True
    # Stored as a plain comma-separated string so pydantic-settings never
    # tries to JSON-parse it. Use the cors_origins property everywhere.
    cors_origins_raw: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")

    # Cookie settings — in production (HTTPS cross-origin) set both to True/"none" via env vars.
    # For local dev keep the defaults (False / "lax").
    cookie_secure: bool = Field(default=False)
    cookie_samesite: str = Field(default="lax")

    # Audit phase A3b: retention window for auth audit log rows. Stored on
    # every inserted row as `retention_until_at = now() + this`. NO auto-pruner
    # is built — actually deleting audit rows requires the A2 maintenance
    # bypass and is a deliberate, audited operation handled in a later phase.
    auth_audit_retention_days: int = Field(default=540)  # 18 months

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() not in _LOCAL_ENVIRONMENTS

    @property
    def jwt_private_key_pem(self) -> str:
        return _pem(self.jwt_private_key)

    @property
    def jwt_public_key_pem(self) -> str:
        return _pem(self.jwt_public_key)
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
    # Dashboard fan-out (NEX-53): DB sessions one load may hold at once, and all
    # loads in the process together. Keep the second well under the pool size.
    dashboard_max_concurrency: int = Field(default=4, ge=1)
    dashboard_max_sessions: int = Field(default=6, ge=1)
    discord_webhook_url: str = ""
    discord_alert_user_id: str = ""
    gold_alert_failure_threshold: int = 3


settings = Settings()
