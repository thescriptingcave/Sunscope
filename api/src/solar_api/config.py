"""Configuration, loaded from the repository-root ``.env``.

Secrets are read once at import and never logged. ``Settings`` is a plain
object rather than a module-level singleton so tests can construct one with
overrides instead of monkeypatching globals.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Repository root when running from a source checkout.
REPO_ROOT = Path(__file__).resolve().parents[3]
#: In the container the source lives at /app/src, so the repo root is /app and
#: the built PWA is mounted at /web. Overridable so one code path serves both.
PWA_DIST = Path(os.environ.get("PWA_DIST", str(REPO_ROOT / "web" / "dist")))
ENV_FILE = Path(os.environ.get("API_ENV_FILE", str(REPO_ROOT / ".env")))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- InfluxDB -----------------------------------------------------------
    influx_url: str = Field(default="https://127.0.0.1:8181", alias="INFLUX_URL")
    influx_db: str = Field(default="solar", alias="INFLUX_DB")
    influx_api_token: str = Field(default="", alias="INFLUX_API_TOKEN")
    #: Preferred over the env var: keeps the token out of the process environment.
    influx_token_file: Path = Field(
        default=REPO_ROOT / "secrets" / "api-read.token", alias="INFLUX_TOKEN_FILE"
    )
    #: Combined CA bundle (system CAs + the self-signed InfluxDB cert).
    influx_ca_bundle: Path | None = Field(
        default=REPO_ROOT / "secrets" / "tls" / "ca-bundle.crt", alias="INFLUX_CA_BUNDLE"
    )
    influx_verify_tls: bool = Field(default=True, alias="INFLUX_VERIFY_TLS")
    influx_timeout_s: float = Field(default=10.0, alias="INFLUX_TIMEOUT_S")

    # -- HTTP server ---------------------------------------------------------
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    # NoDecode is required: without it pydantic-settings tries to JSON-parse the
    # env value (a comma-separated string) before the field validator can run,
    # and raises SettingsError at import time.
    api_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:8000"],
        alias="API_CORS_ORIGINS",
    )

    # -- Auth ----------------------------------------------------------------
    api_secret_key: str = Field(default="", alias="API_SECRET_KEY")
    api_admin_username: str = Field(default="admin", alias="API_ADMIN_USERNAME")
    api_admin_password: str = Field(default="", alias="API_ADMIN_PASSWORD")
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = Field(default=8 * 3600, alias="JWT_TTL_SECONDS")

    # -- Rate limiting -------------------------------------------------------
    login_rate_limit: int = Field(default=10, alias="LOGIN_RATE_LIMIT")
    login_rate_window_s: int = Field(default=300, alias="LOGIN_RATE_WINDOW_S")
    query_rate_limit: int = Field(default=120, alias="QUERY_RATE_LIMIT")
    query_rate_window_s: int = Field(default=60, alias="QUERY_RATE_WINDOW_S")

    # -- Query guard rails ---------------------------------------------------
    max_range_days: int = Field(default=90, alias="MAX_RANGE_DAYS")
    max_rows: int = Field(default=5000, alias="MAX_ROWS")

    # -- Alerting ------------------------------------------------------------
    #: Declarative rule file. A path rather than inline settings so thresholds
    #: can be tuned during commissioning without touching Python.
    alert_rules_file: Path | None = Field(default=None, alias="ALERT_RULES_FILE")
    #: Whether to start the MQTT alert engine. Off in tests, which drive the
    #: engine directly and have no broker.
    alerts_enabled: bool = Field(default=True, alias="ALERTS_ENABLED")

    @field_validator("api_cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept a comma-separated string as well as a list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    def validate_runtime(self) -> list[str]:
        """Return a list of fatal misconfigurations, empty if all good.

        Checked at startup rather than at first request, so a missing secret is
        an immediate, obvious failure instead of a 500 an hour later.
        """
        problems: list[str] = []
        # The token is normally in secrets/api-read.token rather than the
        # environment, so check both before complaining.
        if not (read_secret_file(self.influx_token_file) or self.influx_api_token):
            problems.append(
                "no InfluxDB token: run scripts/gen-secrets.sh, then "
                "`docker compose up influx-init` to create secrets/api-read.token"
            )
        if not self.api_secret_key:
            problems.append("API_SECRET_KEY is not set. Run scripts/gen-secrets.sh.")
        if not self.api_admin_password:
            problems.append("API_ADMIN_PASSWORD is not set. Run scripts/gen-secrets.sh.")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()


def read_secret_file(path: Path) -> str:
    """Read a token from ``secrets/``, preferring it over the environment.

    Grafana provisioning expands environment variables and cannot read files, so
    the datasource uses the ``.env`` copy. Everything else prefers the file,
    which keeps the secret out of the process environment where it would be
    visible to anything that can read ``/proc``.
    """
    try:
        value = path.read_text().strip()
    except OSError:
        return ""
    return value if value and not value.startswith("change-me") else ""
