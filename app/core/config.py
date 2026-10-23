"""Application settings, loaded from the environment (see .env.example)."""

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PLACEHOLDER_SECRET = "change-me-in-production"

# The floor any non-test environment must meet; these are argon2-cffi's own
# defaults, which follow the OWASP guidance for Argon2id.
_MIN_ARGON2_TIME_COST = 3
_MIN_ARGON2_MEMORY_COST = 65536
_MIN_ARGON2_PARALLELISM = 4


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ---
    APP_NAME: str = "Attendance Management Platform"
    APP_ENV: Literal["local", "dev", "staging", "prod", "test"] = "local"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    # "json" emits one object per line for a log shipper to index; "text"
    # stays readable in a terminal and is the default so local runs are
    # unchanged. Deployments set json.
    LOG_FORMAT: Literal["text", "json"] = "text"

    # --- database (Supabase PostgreSQL) ---
    DATABASE_URL: PostgresDsn = Field(
        ...,
        description=(
            "Async SQLAlchemy DSN, e.g. "
            "postgresql+asyncpg://postgres:pwd@db.<ref>.supabase.co:5432/postgres"
        ),
    )
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_PRE_PING: bool = True
    # Supabase's pooler (port 6543) runs pgbouncer in transaction mode, which is
    # incompatible with asyncpg's prepared-statement cache. Set this to True when
    # pointing DATABASE_URL at the pooler.
    DB_USE_PGBOUNCER: bool = False

    # --- supabase project (storage / auth / realtime) ---
    SUPABASE_URL: str | None = None
    SUPABASE_ANON_KEY: str | None = None
    SUPABASE_SERVICE_ROLE_KEY: str | None = None

    # --- auth ---
    JWT_SECRET_KEY: str = _PLACEHOLDER_SECRET
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # --- password hashing (Argon2id) ---
    # Defaults are argon2-cffi's, which track the OWASP recommendation. They
    # are configurable only so the test suite can turn the work factor down:
    # hashing is deliberately expensive, and a suite that creates hundreds of
    # users pays that cost hundreds of times for no security benefit. The
    # validator below refuses weakened parameters outside tests.
    ARGON2_TIME_COST: int = Field(default=3, ge=1)
    ARGON2_MEMORY_COST: int = Field(default=65536, ge=8)
    ARGON2_PARALLELISM: int = Field(default=4, ge=1)

    # --- AI provider (OpenRouter) ---
    OPENROUTER_API_KEY: str | None = None
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "stepfun/step-3.7-flash"
    # Step 3.7 Flash always reasons; the effort level is the only lever.
    OPENROUTER_REASONING_EFFORT: Literal["low", "medium", "high"] = "medium"
    # Without an explicit cap the provider reserves the model's full context
    # (131k for Step 3.7 Flash) against the account balance, which fails with a
    # 402 on a small balance and bounds nothing on a large one. An attendance
    # answer plus its reasoning tokens fits in a fraction of this.
    OPENROUTER_MAX_TOKENS: int = Field(default=2048, ge=256, le=32768)
    OPENROUTER_TIMEOUT_SECONDS: float = 60.0
    OPENROUTER_MAX_RETRIES: int = 2
    # Optional attribution shown on OpenRouter's leaderboards.
    OPENROUTER_SITE_URL: str | None = None
    OPENROUTER_APP_NAME: str | None = None

    # --- CORS / clients ---
    # NoDecode: pydantic-settings JSON-decodes complex fields inside the
    # settings source, which happens *before* field validators run. Opting
    # out lets the validator below accept a plain comma-separated string.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:8501",
        "http://localhost:3000",
    ]
    API_BASE_URL: str = "http://localhost:8000"

    @model_validator(mode="after")
    def _reject_default_secret_in_prod(self) -> "Settings":
        """A deploy must not run on the placeholder signing key."""
        if self.APP_ENV == "prod" and self.JWT_SECRET_KEY == _PLACEHOLDER_SECRET:
            raise ValueError(
                "JWT_SECRET_KEY is still the placeholder value. Generate one "
                'with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self

    @model_validator(mode="after")
    def _reject_wildcard_cors_in_prod(self) -> "Settings":
        """``CORS_ORIGINS=*`` is not what it looks like.

        The API sends ``allow_credentials=True``. Starlette does not refuse a
        credentialed wildcard -- it echoes whatever ``Origin`` the request
        carried back in ``Access-Control-Allow-Origin``, which means *any*
        site can read authenticated responses. The wildcard is reasonable
        while developing, so this only closes it in prod, where the mistake
        would be silent.
        """
        if self.APP_ENV == "prod" and "*" in self.CORS_ORIGINS:
            raise ValueError(
                "CORS_ORIGINS must list explicit origins in prod. With "
                "allow_credentials=True a '*' is echoed back per-request, so "
                "any site could read authenticated responses."
            )
        return self

    @model_validator(mode="after")
    def _reject_weak_hashing_outside_tests(self) -> "Settings":
        """Only ``APP_ENV=test`` may lower the Argon2 work factor.

        Without this guard, the convenience that makes the test suite fast
        would be one stray environment variable away from making production
        password hashes cheap to crack.
        """
        if self.APP_ENV == "test":
            return self

        weakened = (
            self.ARGON2_TIME_COST < _MIN_ARGON2_TIME_COST
            or self.ARGON2_MEMORY_COST < _MIN_ARGON2_MEMORY_COST
            or self.ARGON2_PARALLELISM < _MIN_ARGON2_PARALLELISM
        )
        if weakened:
            raise ValueError(
                "Argon2 parameters are below the minimum safe values "
                f"(time_cost>={_MIN_ARGON2_TIME_COST}, "
                f"memory_cost>={_MIN_ARGON2_MEMORY_COST}, "
                f"parallelism>={_MIN_ARGON2_PARALLELISM}). "
                "Lowering them is only permitted with APP_ENV=test."
            )
        return self

    @field_validator("OPENROUTER_BASE_URL")
    @classmethod
    def _normalise_openrouter_base_url(cls, v: str) -> str:
        """Accept the full chat endpoint where the base URL belongs.

        OpenRouter's docs show ``.../api/v1/chat/completions``, and pasting that
        here is an easy mistake: the OpenAI SDK appends its own path, producing
        ``/chat/completions/chat/completions`` and a bare 404 that says nothing
        about the cause. A base URL can never legitimately end in an endpoint
        path, so trim it rather than failing.
        """
        trimmed = v.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if trimmed.endswith(suffix):
                trimmed = trimmed[: -len(suffix)]
                break
        return trimmed

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_async_driver(cls, v: PostgresDsn) -> PostgresDsn:
        """Fail early and clearly if the DSN names a sync driver.

        Without this the first symptom is a ModuleNotFoundError for psycopg2,
        which says nothing about the actual mistake.
        """
        if v.scheme != "postgresql+asyncpg":
            raise ValueError(
                f"DATABASE_URL must use the asyncpg driver, got "
                f"'{v.scheme}://'. Take the Supabase connection string and "
                f"replace 'postgresql://' with 'postgresql+asyncpg://'."
            )
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """Accept either a comma-separated string or a JSON array in .env."""
        if not isinstance(v, str):
            return v
        v = v.strip()
        if v.startswith("["):
            return json.loads(v)
        return [origin.strip() for origin in v.split(",") if origin.strip()]

    @property
    def database_url(self) -> str:
        return str(self.DATABASE_URL)

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "prod"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor; use as a FastAPI dependency or call directly."""
    return Settings()  # type: ignore[call-arg]
