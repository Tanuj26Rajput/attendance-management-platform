# API reference

A concise reference for the routes in `app/api/v1/routes/`. The full,
auto-generated schema is always available at `/docs` (Swagger UI), `/redoc`
and `/openapi.json` while the API is running.

Base path: **`/api/v1`**. Authenticated requests send a bearer token:

```http
Authorization: Bearer <access_token>
```

Roles: **public** (no token), **authenticated** (any active user), **admin**
(`role == admin`).

---

## Authentication

Login, logout, and "who am I" — issues and revokes the JWTs everything else
depends on.

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `POST /auth/register` | public | creates a new **employee** account (never admin) |
| `POST /auth/login` | public | returns a bearer token |
| `POST /auth/logout` | authenticated | revokes the current token |
| `GET /auth/me` | authenticated | current user's profile |

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ada.whitfield@example.com","password":"Demo!Admin-7fQ2vXpL"}'
```

## Attendance

Punching in/out and reading your own attendance. Identity always comes from
the token — nobody can punch for someone else.

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `POST /attendance/punch-in` | authenticated | start today's record |
| `POST /attendance/punch-out` | authenticated | close today's record |
| `GET /attendance/me` | authenticated | today's record |
| `GET /attendance/me/history` | authenticated | own attendance history |

## Employees and shifts

Admin-only directory management: departments, shifts, and who's assigned to
which shift.

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `GET /admin/users` | admin | list employees |
| `POST/GET/PATCH /admin/departments...` | admin | manage departments |
| `POST/GET/PATCH /admin/shifts...` | admin | manage shifts |
| `POST/GET/PATCH /admin/assignments...` | admin | assign employees to shifts |

```bash
curl -X POST http://localhost:8000/api/v1/admin/shifts \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Night Support","code":"NGT","timezone":"UTC",
       "start_time":"22:00:00","end_time":"06:00:00","is_overnight":true}'
```

Overlapping shift assignments for the same employee are rejected (enforced
by a database constraint, not just application code).

## Reports and analytics

Admin-only. Every figure here is computed in Python/SQL — the AI assistant
is never involved in these numbers.

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `GET /reports/daily` | admin | one day's summary |
| `GET /reports/daily-summaries` | admin | summaries over a date range |
| `GET /reports/trends` | admin | attendance trend by day/week/month |
| `GET /reports/late-arrivals` | admin | who was late, and by how much |
| `GET /reports/absences` | admin | derived absences (no record, no approved leave) |
| `GET /reports/absenteeism` | admin | absenteeism rate by department or employee |

```bash
curl -G http://localhost:8000/api/v1/reports/absenteeism \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  --data-urlencode 'start=2026-10-01' --data-urlencode 'end=2026-10-31'
```

## Chatbot

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `POST /chat` | authenticated | ask a question about attendance data |

```json
{
  "answer": "You were late twice in the last 30 days.",
  "grounded": true,
  "tools_used": [{ "name": "attendance_summary", "ok": true }]
}
```

The assistant answers only using real database queries run through a fixed
set of read-only tools (attendance summary, late arrivals, absences, trends,
leave requests, shift info). A non-admin can only ask about their own data.
`grounded: false` means the answer wasn't backed by a real lookup (e.g. a
greeting or a refusal). Returns **503** if no AI provider key is configured.

## Audit logs

| Endpoint | Role | Purpose |
| --- | --- | --- |
| `GET /admin/audit-logs` | admin | read the activity trail |

There is no write endpoint — audit rows are created automatically as a
side effect of other actions, and the table is append-only (a database
trigger blocks edits and deletes).

---

## Common errors

| Status | Meaning |
| --- | --- |
| 401 | missing/invalid/expired token, or bad login |
| 403 | authenticated but not permitted |
| 404 | record not found |
| 409 | conflict (duplicate punch, duplicate email, overlapping assignment) |
| 422 | validation failed |
| 503 | AI provider unavailable |

## Known limitations

- **Leave has no REST endpoint.** Leave requests are modelled and used by
  reports, but there's no route to create, list, approve or reject one yet.
- No endpoint to correct an attendance record, change a role, or delete a
  user.
- `/docs`, `/redoc` and `/openapi.json` are served even in production.
