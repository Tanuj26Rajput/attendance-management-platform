"""Settings loading behaviour."""

import pytest

from app.core.config import _PLACEHOLDER_SECRET, Settings

DSN = "postgresql+asyncpg://u:p@localhost:5432/db"

# conftest lowers the Argon2 work factor for the whole test run, which the
# config guard only permits under APP_ENV=test. Any Settings built here with a
# different APP_ENV has to restate production-grade parameters, so these are
# spelled out rather than inherited from the environment.
STRONG_ARGON2 = {
    "ARGON2_TIME_COST": 3,
    "ARGON2_MEMORY_COST": 65536,
    "ARGON2_PARALLELISM": 4,
}


def test_cors_origins_accepts_comma_separated_string() -> None:
    settings = Settings(
        DATABASE_URL=DSN,
        CORS_ORIGINS="http://a.test, http://b.test",
    )
    assert settings.CORS_ORIGINS == ["http://a.test", "http://b.test"]


def test_is_production_flag() -> None:
    settings = Settings(
        DATABASE_URL=DSN,
        APP_ENV="prod",
        JWT_SECRET_KEY="a-real-secret-for-this-test-only",
        **STRONG_ARGON2,
    )
    assert settings.is_production is True


def test_production_rejects_the_placeholder_secret() -> None:
    # Passed explicitly: conftest puts a real-looking secret in the environment,
    # so the placeholder has to be restated to exercise the guard.
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(
            DATABASE_URL=DSN,
            APP_ENV="prod",
            JWT_SECRET_KEY=_PLACEHOLDER_SECRET,
            **STRONG_ARGON2,
        )


def test_sync_database_driver_is_rejected() -> None:
    """Supabase hands out the sync DSN; failing here beats a psycopg2 import error."""
    with pytest.raises(ValueError, match="asyncpg"):
        Settings(DATABASE_URL="postgresql://u:p@localhost:5432/db")


# ------------------------------------------------------- argon2 work factor
@pytest.mark.parametrize(
    "weakened",
    [
        {"ARGON2_TIME_COST": 1},
        {"ARGON2_MEMORY_COST": 8},
        {"ARGON2_PARALLELISM": 1},
    ],
)
@pytest.mark.parametrize("env", ["local", "dev", "staging", "prod"])
def test_weak_argon2_parameters_are_rejected_outside_tests(weakened, env) -> None:
    """The speed-up that makes the suite fast must not reach a real deploy."""
    with pytest.raises(ValueError, match="Argon2"):
        Settings(
            DATABASE_URL=DSN,
            APP_ENV=env,
            JWT_SECRET_KEY="a-real-secret-for-this-test-only",
            **{**STRONG_ARGON2, **weakened},
        )


def test_weak_argon2_parameters_are_allowed_in_tests() -> None:
    settings = Settings(
        DATABASE_URL=DSN,
        APP_ENV="test",
        ARGON2_TIME_COST=1,
        ARGON2_MEMORY_COST=8,
        ARGON2_PARALLELISM=1,
    )
    assert settings.ARGON2_TIME_COST == 1


def test_default_argon2_parameters_meet_the_floor() -> None:
    """The shipped defaults must not themselves trip the guard.

    Read off the field defaults rather than an instance: conftest exports weak
    values for the suite, and environment variables outrank defaults.
    """
    defaults = {
        name: Settings.model_fields[name].default
        for name in ("ARGON2_TIME_COST", "ARGON2_MEMORY_COST", "ARGON2_PARALLELISM")
    }
    assert defaults["ARGON2_TIME_COST"] >= 3
    assert defaults["ARGON2_MEMORY_COST"] >= 65536
    assert defaults["ARGON2_PARALLELISM"] >= 4

    # And a deploy that sets nothing must therefore be accepted.
    settings = Settings(DATABASE_URL=DSN, APP_ENV="local", **defaults)
    assert settings.ARGON2_TIME_COST >= 3


# -------------------------------------------------- openrouter base url
@pytest.mark.parametrize(
    "supplied",
    [
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
        "https://openrouter.ai/api/v1/chat/completions",
        "  https://openrouter.ai/api/v1/chat/completions/  ",
    ],
)
def test_openrouter_base_url_is_normalised(supplied) -> None:
    """Pasting the full endpoint is the obvious mistake; it must still work.

    Left alone it produces /chat/completions/chat/completions and a bare 404.
    """
    settings = Settings(DATABASE_URL=DSN, OPENROUTER_BASE_URL=supplied)
    assert settings.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
