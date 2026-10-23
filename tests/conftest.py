"""Shared pytest fixtures.

Environment defaults are set before the application is imported so Settings
validation succeeds. A local .env is respected when present -- integration
tests run against whatever database it points at, inside a transaction that is
always rolled back.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-not-used-in-production")

# Argon2 is deliberately slow (~105ms a hash). The suite creates hundreds of
# users, so the real work factor cost minutes per run and bought nothing --
# only APP_ENV=test is allowed to lower it (see Settings._reject_weak_hashing).
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_MEMORY_COST", "8")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

# Only invent a DATABASE_URL when nothing else supplies one; an env var set by
# CI, or a local .env, must win so integration tests hit a real database.
if not pathlib.Path(".env").exists():
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/attendance_test",
    )

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.main import create_app
from app.models.enums import UserRole
from app.models.shift import EmployeeShiftAssignment, Shift
from app.models.user import User


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """HTTP client bound to the ASGI app; no network socket is opened."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# database-backed fixtures (integration)
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(settings) -> AsyncGenerator[AsyncEngine, None]:
    """One engine for the whole run.

    Connecting to a hosted database costs seconds (TLS plus pooler auth), so
    building an engine per test dominated the suite's runtime. ``pool_pre_ping``
    discards a connection the pooler has dropped underneath us instead of
    failing a test with ``ConnectionDoesNotExistError``.
    """
    url = os.getenv("TEST_DATABASE_URL") or settings.database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """A session whose work is always rolled back.

    The session joins an outer transaction via a savepoint, so service code
    calling ``commit()`` behaves normally while the outer rollback still
    discards everything. Tests leave no rows behind.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = maker()
        try:
            yield session
        finally:
            await session.close()
            if transaction.is_active:
                await transaction.rollback()


@pytest.fixture
async def api(
    app: FastAPI, db_session: AsyncSession
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Client whose requests all share the rolled-back test session."""

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _unique_email(prefix: str = "user") -> str:
    # .test is a reserved TLD that email-validator rejects; example.com is
    # the RFC 2606 domain reserved for documentation and testing.
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def unique_email():
    return _unique_email


@pytest.fixture
async def make_user(db_session: AsyncSession):
    """Insert a user directly -- the out-of-band path admins are created by."""

    async def _make(
        *,
        password: str = "a-perfectly-fine-password",
        role: UserRole = UserRole.EMPLOYEE,
        is_active: bool = True,
        email: str | None = None,
    ) -> User:
        user = User(
            email=email or _unique_email(role.value),
            full_name=f"Test {role.value}",
            hashed_password=hash_password(password),
            employee_code=f"EMP-{uuid.uuid4().hex[:8].upper()}",
            role=role,
            is_active=is_active,
        )
        db_session.add(user)
        await db_session.flush()
        return user

    return _make


@pytest.fixture
async def make_shift(db_session: AsyncSession):
    """Create a Shift. Defaults run every day so tests are date-independent."""

    async def _make(
        *,
        start_time: dt.time | None = None,
        end_time: dt.time | None = None,
        is_overnight: bool = False,
        break_minutes: int = 0,
        grace_period_minutes: int = 15,
        half_day_threshold_minutes: int = 240,
        working_days: list[int] | None = None,
        timezone: str = "UTC",
    ) -> Shift:
        suffix = uuid.uuid4().hex[:8]
        shift = Shift(
            name=f"Shift {suffix}",
            code=f"S{suffix[:6].upper()}",
            timezone=timezone,
            start_time=start_time or dt.time(9, 0),
            end_time=end_time or dt.time(18, 0),
            is_overnight=is_overnight,
            break_minutes=break_minutes,
            grace_period_minutes=grace_period_minutes,
            half_day_threshold_minutes=half_day_threshold_minutes,
            working_days=(
                working_days if working_days is not None else [1, 2, 3, 4, 5, 6, 7]
            ),
        )
        db_session.add(shift)
        await db_session.flush()
        return shift

    return _make


@pytest.fixture
async def assign_shift(db_session: AsyncSession):
    """Attach a shift to a user from a date (default: a year ago)."""

    async def _assign(
        user: User, shift: Shift, *, effective_from: dt.date | None = None
    ) -> EmployeeShiftAssignment:
        assignment = EmployeeShiftAssignment(
            user_id=user.id,
            shift_id=shift.id,
            effective_from=(effective_from or dt.date.today() - dt.timedelta(days=365)),
            is_primary=True,
        )
        db_session.add(assignment)
        await db_session.flush()
        return assignment

    return _assign
