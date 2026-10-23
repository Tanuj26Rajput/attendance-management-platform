"""Liveness and readiness probes."""

from fastapi import APIRouter, status
from pydantic import BaseModel
from sqlalchemy import text

from app import __version__
from app.api.deps import SessionDep, SettingsDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    database: str


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness: the process is up. Does not touch the database."""
    return HealthResponse(
        status="ok", version=__version__, environment=settings.APP_ENV
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
)
async def readiness(session: SessionDep) -> ReadinessResponse:
    """Readiness: verifies the database answers a trivial query."""
    await session.execute(text("SELECT 1"))
    return ReadinessResponse(status="ok", database="reachable")
