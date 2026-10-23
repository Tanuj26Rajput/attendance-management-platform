"""Audit trail tests: coverage, RBAC, append-only, leakage.

Integration: needs PostgreSQL. Each test runs in a rolled-back transaction.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text

from app.models.audit import AuditLog
from app.models.enums import AuditAction, UserRole

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-fine-password"
MON = dt.date(2026, 5, 4)
WED = dt.date(2026, 5, 6)
EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]


async def _token(api: httpx.AsyncClient, user) -> str:
    response = await api.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin(make_user):
    return await make_user(password=PASSWORD, role=UserRole.ADMIN)


@pytest.fixture
async def admin_headers(api, admin):
    return _auth(await _token(api, admin))


@pytest.fixture
async def employee(make_user, make_shift, assign_shift):
    user = await make_user(password=PASSWORD)
    shift = await make_shift(working_days=EVERY_DAY)
    await assign_shift(user, shift, effective_from=MON - dt.timedelta(days=60))
    return user


async def _logs(db_session, **filters) -> list[AuditLog]:
    stmt = select(AuditLog)
    for key, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, key) == value)
    return list((await db_session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------- RBAC
async def test_audit_logs_require_authentication(api) -> None:
    assert (await api.get("/api/v1/admin/audit-logs")).status_code == 401


async def test_audit_logs_reject_employees(api, employee) -> None:
    """The audit trail records what everyone did; only admins may read it."""
    headers = _auth(await _token(api, employee))
    assert (
        await api.get("/api/v1/admin/audit-logs", headers=headers)
    ).status_code == 403


async def test_admin_can_read_the_trail(api, admin_headers) -> None:
    response = await api.get("/api/v1/admin/audit-logs", headers=admin_headers)
    assert response.status_code == 200
    assert set(response.json()) == {"items", "total", "limit", "offset"}


# ----------------------------------------------------------- append-only
@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE"])
async def test_no_endpoint_mutates_the_trail(api, admin_headers, method) -> None:
    """There is deliberately no write counterpart to the read endpoint."""
    response = await api.request(
        method,
        f"/api/v1/admin/audit-logs/{uuid.uuid4()}",
        headers=admin_headers,
        json={"summary": "tampered"},
    )
    assert response.status_code in (404, 405)


async def test_database_rejects_updating_an_audit_row(db_session, admin) -> None:
    """Convention is not enough: the database itself refuses the write.

    This is what makes the trail evidence rather than a log -- an admin with
    database access still cannot quietly rewrite what they did.
    """
    row = AuditLog(
        actor_id=admin.id,
        action=AuditAction.CREATE,
        entity_type="probe",
        summary="original",
    )
    db_session.add(row)
    await db_session.commit()

    with pytest.raises(Exception, match="append-only"):
        await db_session.execute(
            text("UPDATE audit_logs SET summary = 'tampered' WHERE id = :i"),
            {"i": row.id},
        )
    await db_session.rollback()


async def test_database_rejects_deleting_an_audit_row(db_session, admin) -> None:
    row = AuditLog(
        actor_id=admin.id,
        action=AuditAction.CREATE,
        entity_type="probe",
        summary="keep me",
    )
    db_session.add(row)
    await db_session.commit()

    with pytest.raises(Exception, match="append-only"):
        await db_session.execute(
            text("DELETE FROM audit_logs WHERE id = :i"), {"i": row.id}
        )
    await db_session.rollback()


# ------------------------------------------------------- event coverage
async def test_punches_are_audited(api, employee, db_session) -> None:
    headers = _auth(await _token(api, employee))
    await api.post("/api/v1/attendance/punch-in", json={}, headers=headers)
    await api.post("/api/v1/attendance/punch-out", json={}, headers=headers)

    rows = await _logs(db_session, entity_type="attendance_record")
    assert {r.action for r in rows} == {AuditAction.CREATE, AuditAction.UPDATE}


async def test_shift_changes_are_audited(api, admin, admin_headers, db_session) -> None:
    sfx = uuid.uuid4().hex[:8]
    created = (
        await api.post(
            "/api/v1/admin/shifts",
            json={
                "name": f"Audited {sfx}",
                "code": f"A{sfx[:6]}",
                "start_time": "09:00:00",
                "end_time": "18:00:00",
            },
            headers=admin_headers,
        )
    ).json()
    await api.patch(
        f"/api/v1/admin/shifts/{created['id']}",
        json={"grace_period_minutes": 5},
        headers=admin_headers,
    )

    rows = await _logs(db_session, entity_type="shift")
    assert {r.action for r in rows} == {AuditAction.CREATE, AuditAction.UPDATE}
    assert all(r.actor_id == admin.id for r in rows)


async def test_report_access_is_audited(api, admin, admin_headers, db_session) -> None:
    """Reading other people's attendance is itself an auditable event."""
    await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )

    rows = await _logs(db_session, entity_type="report")
    assert len(rows) == 1
    assert rows[0].action is AuditAction.READ
    assert rows[0].actor_id == admin.id
    assert rows[0].changes["report"] == "absences"
    # The filters used are recorded, so a past read can be reproduced.
    assert rows[0].changes["parameters"]["start"] == MON.isoformat()


async def test_each_report_records_its_own_access(api, admin_headers, db_session):
    for path in (
        f"/api/v1/reports/daily?on={MON}",
        f"/api/v1/reports/trends?start={MON}&end={WED}",
        f"/api/v1/reports/absenteeism?start={MON}&end={WED}",
    ):
        assert (await api.get(path, headers=admin_headers)).status_code == 200

    rows = await _logs(db_session, entity_type="report")
    assert {r.changes["report"] for r in rows} == {"daily", "trends", "absenteeism"}


async def test_a_rejected_report_request_is_not_audited(
    api, admin_headers, db_session
) -> None:
    """An invalid range never reaches the data, so there is no access to record."""
    await api.get(
        f"/api/v1/reports/absences?start={WED}&end={MON}", headers=admin_headers
    )
    assert await _logs(db_session, entity_type="report") == []


# ------------------------------------------------------------ chat audit
class _FakeLLM:
    def __init__(self, message):
        self._message = message
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


async def test_chat_queries_are_audited_without_the_question(
    api, employee, db_session, monkeypatch
) -> None:
    """The fact of the query is recorded; what was asked is not."""
    from app.services import chat as chat_service

    monkeypatch.setattr(
        chat_service,
        "get_client",
        lambda: _FakeLLM(SimpleNamespace(content="I cannot say.", tool_calls=None)),
    )

    secret = "why was I marked absent on my hospital appointment day"
    headers = _auth(await _token(api, employee))
    response = await api.post("/api/v1/chat", json={"message": secret}, headers=headers)
    assert response.status_code == 200, response.text

    rows = await _logs(db_session, entity_type="chat")
    assert len(rows) == 1
    blob = json.dumps(rows[0].changes) + (rows[0].summary or "")
    assert "hospital" not in blob
    assert secret not in blob
    assert rows[0].changes["question_chars"] == len(secret)
    assert rows[0].action is AuditAction.QUERY


async def test_an_unavailable_assistant_is_still_audited(
    api, employee, db_session, monkeypatch
) -> None:
    from app.ai import client as ai_client
    from app.services import chat as chat_service

    def _boom():
        raise ai_client.MissingAPIKeyError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(chat_service, "get_client", _boom)

    headers = _auth(await _token(api, employee))
    response = await api.post(
        "/api/v1/chat", json={"message": "Was I late?"}, headers=headers
    )
    assert response.status_code == 503

    rows = await _logs(db_session, entity_type="chat")
    assert rows[0].changes["outcome"] == "unavailable"
    assert rows[0].changes["grounded"] is False


# --------------------------------------------------------- no leakage
async def test_audit_rows_never_contain_credentials(api, employee, db_session) -> None:
    """Registration and login must not leave a password anywhere in the trail."""
    email = f"leak-{uuid.uuid4().hex[:10]}@example.com"
    await api.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "a-very-secret-password",
            "full_name": "Leak Probe",
        },
    )
    await api.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "a-very-secret-password"},
    )

    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    blob = json.dumps([{"s": r.summary, "c": r.changes} for r in rows], default=str)
    assert "a-very-secret-password" not in blob
    assert "argon2" not in blob.lower()
    assert "hashed_password" not in blob


async def test_report_audit_parameters_are_redacted(
    api, admin_headers, db_session
) -> None:
    """Filters are recorded, but anything that looks personal is scrubbed."""
    await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    rows = await _logs(db_session, entity_type="report")
    blob = json.dumps(rows[0].changes)
    assert "@" not in blob


# -------------------------------------------------------------- reading
async def test_audit_logs_filter_by_entity_type(
    api, admin, admin_headers, db_session
) -> None:
    await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )

    response = await api.get(
        "/api/v1/admin/audit-logs?entity_type=report", headers=admin_headers
    )
    body = response.json()
    assert body["total"] >= 1
    assert all(item["entity_type"] == "report" for item in body["items"])


async def test_audit_logs_filter_by_action(api, admin_headers) -> None:
    response = await api.get(
        "/api/v1/admin/audit-logs?action=read", headers=admin_headers
    )
    assert response.status_code == 200
    assert all(item["action"] == "read" for item in response.json()["items"])


async def test_audit_logs_reject_an_inverted_date_range(api, admin_headers) -> None:
    response = await api.get(
        "/api/v1/admin/audit-logs?start=2026-05-10T00:00:00Z&end=2026-05-01T00:00:00Z",
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_audit_timestamps_are_timezone_aware(
    api, admin, admin_headers, db_session
) -> None:
    """Stored in UTC, and serialised with an offset so a client cannot guess wrong."""
    await api.get(
        f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
    )
    response = await api.get(
        "/api/v1/admin/audit-logs?entity_type=report", headers=admin_headers
    )
    stamp = dt.datetime.fromisoformat(response.json()["items"][0]["created_at"])
    assert stamp.tzinfo is not None
    assert (
        abs((stamp.astimezone(dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds()) < 300
    )


async def test_audit_log_listing_paginates(api, admin_headers) -> None:
    for _ in range(3):
        await api.get(
            f"/api/v1/reports/absences?start={MON}&end={WED}", headers=admin_headers
        )

    page = await api.get(
        "/api/v1/admin/audit-logs?entity_type=report&limit=2", headers=admin_headers
    )
    body = page.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
