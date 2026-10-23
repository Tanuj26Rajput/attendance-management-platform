"""Smoke tests for the scaffold: the app boots and its probes respond."""

import httpx
import pytest

from app import __version__


async def test_root_returns_service_metadata(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    assert body["docs"] == "/docs"


async def test_health_reports_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"


async def test_openapi_schema_is_generated(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]


@pytest.mark.integration
async def test_readiness_requires_database(client: httpx.AsyncClient) -> None:
    """Deselected by default; run with `pytest -m integration` against a DB."""
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "reachable"
