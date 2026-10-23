# API reference

Every route below exists in `app/api/v1/routes/`. The source of truth is the
running application: FastAPI generates an OpenAPI document from the same
handlers and Pydantic models this page describes.

- **Interactive docs:** `GET /docs` (Swagger UI)
- **ReDoc:** `GET /redoc`
- **Machine-readable schema:** `GET /openapi.json`

All three are configured unconditionally in `create_app()` (`app/main.py`), so
they are served in **every** environment, including production. See
[Known limitations](#known-limitations) below.

Base path: **`/api/v1`** (`API_V1_PREFIX`). Every path in this document is
relative to it except the health probes, which are mounted at the same prefix
(`/api/v1/health`).

---

## Authentication

A bearer JWT, issued by `POST /auth/login`:

```http
Authorization: Bearer <access_token>
```

The token carries `sub` (user id), `role`, `jti`, `iat`, `exp` and `type`. It is
signed with `JWT_SECRET_KEY` using `JWT_ALGORITHM` (default `HS256`) and expires
after `ACCESS_TOKEN_EXPIRE_MINUTES` (default 60).

Every authenticated request goes through `app/api/deps.py`, which:

1. rejects a missing or malformed `Authorization` header with **401**;
2. rejects an expired token with **401** `Token has expired`;
3. checks the token's `jti` against `revoked_tokens` — a logged-out token is
   **401** `Token has been revoked`;
4. **reloads the user from the database** rather than trusting the token's
   claims, so deactivating an account takes effect on the next request;
5. rejects an inactive account with **403** `Inactive user account`.

### Roles

`UserRole` has exactly two members: `employee` and `admin`. There is no manager
or supervisor role.

| Marker in this document | Dependency | Meaning |
| --- | --- | --- |
| *public* | — | no token required |
| *authenticated* | `CurrentUser` | any active user, employee or admin |
| *admin* | `CurrentAdmin` | `role == admin`, else **403** |

`CurrentAdmin` is `require_roles(UserRole.ADMIN)`. Authorisation is declared as
a dependency, never called inside a handler, so a route cannot be left
unprotected by forgetting a check.

---

## Conventions

### Pagination

Every listing takes `limit` and `offset` and returns the same envelope
(`app/schemas/common.py`):

```json
{
  "items": [],
  "total": 0,
  "limit": 50,
  "offset": 0
}
```

`total` is the count matching the filters, not the size of the page, so a
client can tell whether more pages exist without probing for an empty one.

Defaults and ceilings are per-route and enforced by FastAPI `Query`
constraints. Most listings are `limit` 1–200 defaulting to 50; `/reports/trends`
allows 1–400 defaulting to 100. A `limit` outside the range is a **422**.

`GET /attendance/me/history` returns `AttendanceHistoryResponse`, which has the
same four fields but is a named model rather than the generic `Page`.

### Errors

| Status | When |
| --- | --- |
| **400** | defensive fallback for an untranslated domain error |
| **401** | missing, malformed, expired or revoked token; bad login |
| **403** | authenticated but not permitted, or the account is inactive |
| **404** | no such record (`/attendance/me` with no record today; admin `Not found`) |
| **409** | a state conflict — duplicate punch, duplicate email, overlapping assignment |
| **422** | request validation failed, or a business rule rejected the input |
| **503** | the assistant's upstream provider is unavailable |

Application errors return FastAPI's standard shape:

```json
{ "detail": "You are already punched in for today" }
```

Validation errors return FastAPI's list form:

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "password"],
      "msg": "String should have at least 12 characters",
      "input": "short"
    }
  ]
}
```

Admin error messages are written for an operator and never echo a database
error, so a constraint or table name cannot leak through a 409
(`_translate()` in `app/api/v1/routes/admin.py`).

Every write schema sets `extra="forbid"`, so an unknown key — an admin's typo
like `grace_minutes` — is a **422** rather than a silently ignored field.

### Timestamps

Stored and returned in **UTC**. Shift `start_time` / `end_time` are naive
wall-clock times read in the shift's own `timezone`.

---

## Health

#### `GET /api/v1/health` — *public*

Liveness. Does not touch the database.

```json
{ "status": "ok", "version": "0.1.0", "environment": "local" }
```

#### `GET /api/v1/health/ready` — *public*

Readiness. Runs `SELECT 1`.

```json
{ "status": "ok", "database": "reachable" }
```

---

## Auth

#### `POST /auth/register` — *public* → **201**

Creates an **employee**. There is no way to obtain an admin account here: the
schema has no `role` field and forbids extras, and `register_user()` hard-codes
`UserRole.EMPLOYEE` without reading role input at all. Admins are provisioned
out of band.

Body (`UserRegister`):

| Field | Type | Rules |
| --- | --- | --- |
| `email` | `EmailStr` | lower-cased before storage |
| `password` | `str` | 12–128 characters |
| `full_name` | `str` | 1–160 characters |

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"new.hire@example.com","password":"correct-horse-battery","full_name":"New Hire"}'
```

Returns `UserPublic`. **409** if the email is already registered.

#### `POST /auth/login` — *public* → **200**

Body (`UserLogin`): `email`, `password`.

```json
{ "access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600 }
```

**401** `Incorrect email or password` — the same response for an unknown email
and a wrong password, and the service verifies against a dummy hash when no
user exists so the timing matches. **403** for an inactive account.

#### `POST /auth/logout` — *authenticated* → **200**

Records the presented token's `jti` in `revoked_tokens`. Returns
`{"detail": "Token revoked"}`.

#### `GET /auth/me` — *authenticated* → **200**

Returns `UserPublic`: `id`, `email`, `employee_code`, `full_name`, `role`,
`employment_status`, `is_active`, `department_id`. **Never** includes the
password hash.

---

## Attendance

Identity always comes from the bearer token. No attendance payload accepts a
`user_id`, so one employee cannot punch for another.

#### `POST /attendance/punch-in` — *authenticated* → **201**

Body (`PunchInRequest`, all optional): `source` (`web` | `mobile` | `biometric`
| `import` | `manual`, default `web`), `latitude` (−90…90), `longitude`
(−180…180), `notes` (≤1000 chars).

```bash
curl -X POST http://localhost:8000/api/v1/attendance/punch-in \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"source":"web"}'
```

| Status | Detail |
| --- | --- |
| 409 | `You are already punched in for today` |
| 409 | `Attendance for today is already complete` |
| 422 | `Today is not a working day for your assigned shift` |
| 422 | `You have no active shift assignment` |

#### `POST /attendance/punch-out` — *authenticated* → **200**

Body (`PunchOutRequest`): `notes` (≤1000 chars, optional).

| Status | Detail |
| --- | --- |
| 409 | `You have already punched out for that day` |
| 409 | `You have no open punch-in to close` |
| 422 | `The open record has no shift to evaluate against` |

#### `GET /attendance/me` — *authenticated* → **200**

Today's record. **404** `No attendance record for today` when there is none —
the dashboard treats that as an empty state, not an error.

#### `GET /attendance/me/history` — *authenticated* → **200**

Own history only; there is no parameter to read anyone else's.

Query: `start` (date, optional), `end` (date, optional), `limit` (1–200,
default 50), `offset` (≥0). `start > end` is a **422**.

`AttendanceRecordPublic`:

| Field | Type |
| --- | --- |
| `id` | UUID |
| `work_date` | date |
| `shift_id` | UUID \| null |
| `check_in_at`, `check_out_at` | datetime (UTC) \| null |
| `status` | `present` `absent` `late` `half_day` `on_leave` `holiday` `weekend` |
| `source` | `web` `mobile` `biometric` `import` `manual` |
| `worked_minutes` | int \| null |
| `late_minutes`, `early_leave_minutes`, `overtime_minutes` | int |
| `notes` | str \| null |
| `is_open` | bool — computed: checked in, not yet out |

---

## Admin — directory

Every route in this section is **admin**-only.

### Users

#### `GET /admin/users` → `Page[UserPublic]`

Query: `limit` (1–200, default 50), `offset` (≥0). Ordered by `created_at`
descending.

### Departments

#### `POST /admin/departments` → **201** `DepartmentPublic`

Body (`DepartmentCreate`): `name` (1–120), `code` (2–20, `[A-Za-z0-9_-]+`,
upper-cased on the way in), `description` (≤2000, optional), `head_id` (UUID,
optional). **409** on a duplicate name or code.

#### `GET /admin/departments` → `Page[DepartmentPublic]`

Query: `is_active` (bool), `search` (≤120 chars, matches name or code),
`limit`, `offset`. `search` is a case-insensitive contains match; `%`, `_` and
`\` in the term are escaped and matched literally.

#### `PATCH /admin/departments/{department_id}` → `DepartmentPublic`

Body (`DepartmentUpdate`): any of `name`, `code`, `description`, `head_id`. A
body that sets nothing is a **422**, not a no-op. **404** / **409** as above.

#### `POST /admin/departments/{department_id}/deactivate` → `DepartmentPublic`

Sets `is_active = false`. Departments are deactivated, never deleted —
employees and attendance history reference them.

### Shifts

#### `POST /admin/shifts` → **201** `ShiftPublic`

Body (`ShiftCreate`):

| Field | Type | Rules |
| --- | --- | --- |
| `name` | str | 1–120, unique |
| `code` | str | 2–20, `[A-Za-z0-9_-]+`, upper-cased, unique |
| `description` | str \| null | ≤2000 |
| `timezone` | str | must be a known IANA zone, default `UTC` |
| `start_time`, `end_time` | time | naive wall-clock, read in `timezone` |
| `is_overnight` | bool | default false |
| `break_minutes` | int | 0–1440, default 60 |
| `grace_period_minutes` | int | 0–1440, default 15 |
| `half_day_threshold_minutes` | int | 1–1440, default 240 |
| `working_days` | int[] | ISO weekdays 1–7, no duplicates, sorted; `[]` means every day; default `[1,2,3,4,5]` |

The window must be unambiguous: a non-overnight shift needs `end_time >
start_time`, and an overnight shift needs `end_time <= start_time`. Mismatches
are **422**. `is_overnight` is stored rather than inferred because 09:00–09:00
is otherwise ambiguous.

```bash
curl -X POST http://localhost:8000/api/v1/admin/shifts \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Night Support","code":"NGT","timezone":"UTC",
       "start_time":"22:00:00","end_time":"06:00:00","is_overnight":true,
       "grace_period_minutes":20,"working_days":[1,2,3,4,5]}'
```

#### `GET /admin/shifts` → `Page[ShiftPublic]`

Query: `is_active`, `search`, `limit`, `offset` — same semantics as departments.

#### `PATCH /admin/shifts/{shift_id}` → `ShiftPublic`

Partial update. The window is re-validated against the **merged** result, not
the patch alone, because moving only `end_time` can still invert a window.

#### `POST /admin/shifts/{shift_id}/deactivate` → `ShiftPublic`

Retires the shift. Attendance history that references it is preserved — the FK
from `employee_shift_assignments` is `ON DELETE RESTRICT`, so a shift that has
been worked cannot vanish.

### Shift assignments

#### `POST /admin/assignments` → **201** `AssignmentPublic`

Body (`AssignmentCreate`): `user_id`, `shift_id`, `effective_from`,
`effective_to` (nullable — open-ended), `is_primary` (default true).
`effective_to < effective_from` is a **422**.

**409** on a conflicting assignment. Overlap is enforced in the database by a
GiST `EXCLUDE` constraint over `(user_id, daterange(effective_from,
effective_to))`, plus a partial unique index allowing at most one open-ended
primary assignment per employee — so two concurrent requests cannot both win.

#### `GET /admin/assignments` → `Page[AssignmentPublic]`

Query: `user_id`, `shift_id`, `active_on` (date — only assignments in force on
that day), `limit`, `offset`.

#### `PATCH /admin/assignments/{assignment_id}` → `AssignmentPublic`

Only `effective_to` and `is_primary` may change. Repointing an assignment at a
different shift is deliberately not offered: closing the current assignment and
opening a new one leaves the history showing what was actually in force.

#### `POST /admin/assignments/{assignment_id}/end` → `AssignmentPublic`

Closes the assignment.

### Audit trail

#### `GET /admin/audit-logs` → `Page[AuditLogPublic]`

Admin-only, newest first. There is deliberately **no** POST, PATCH or DELETE
counterpart: the trail is written as a side effect of the action it describes,
and a database trigger rejects `UPDATE` and `DELETE` on `audit_logs` outright.

Query: `actor_id`, `entity_type` (≤64 chars), `action`, `start` (datetime, UTC),
`end` (datetime, UTC), `limit`, `offset`. `start > end` is a **422**.

`action` is one of `create` `update` `delete` `login` `logout` `approve`
`reject` `export` `read` `query`.

`AuditLogPublic`: `id`, `actor_id`, `action`, `entity_type`, `entity_id`,
`summary`, `changes` (JSONB, typically `{"before": …, "after": …}`),
`ip_address`, `created_at`.

---

## Reports

Every route here is **admin**-only. There is no employee-facing variant;
employees read their own data through `/attendance/me*`.

All arithmetic is deterministic Python/SQL in `app/services/reports.py` — no
model is involved in producing a number. Every call writes a `read` audit row
recording who looked and with which filters (the filters, never the results).

Shared query parameters: `start` and `end` are **required inclusive dates**;
ranges longer than 366 days are a **422**. `department_id` narrows the
population.

**Expected** means: the employee had an assignment covering that day, the day
is a working day for the shift, and the shift has already finished in the
shift's timezone. **An absence is an expected working day with no attendance
record and no approved leave** — absences are derived, not stored.

Rates are fractions rounded to 4 decimal places (`0.9167`), not percentages.

#### `GET /reports/daily` → `DailySummaryPublic`

Query: `on` (date, required), `department_id`. Zero-filled when nobody was
expected, rather than 404 — "no one was scheduled" is a real answer.

```json
{
  "work_date": "2026-10-20", "expected": 10, "present": 7, "late": 2,
  "half_day": 0, "absent": 1, "on_leave": 0,
  "attendance_rate": 0.9, "absence_rate": 0.1
}
```

#### `GET /reports/daily-summaries` → `Page[DailySummaryPublic]`

Query: `start`, `end`, `department_id`, `limit` (1–200, default 50), `offset`.

#### `GET /reports/trends` → `Page[TrendPointPublic]`

Query: `start`, `end`, `granularity` (`day` | `week` | `month`, default `day`),
`department_id`, `limit` (1–400, default 100), `offset`.

Each point: `period_start`, `period_end`, `expected`, `attended`, `late`,
`absent`, `on_leave`, `attendance_rate`, `absence_rate`,
`average_late_minutes`.

#### `GET /reports/late-arrivals` → `Page[EmployeeDayPublic]`

Query: `start`, `end`, `department_id`, `user_id`, `min_late_minutes` (1–1440,
default 1), `limit`, `offset`.

#### `GET /reports/absences` → `Page[EmployeeDayPublic]`

Query: `start`, `end`, `department_id`, `user_id`, `limit`, `offset`.

`EmployeeDayPublic`: `user_id`, `employee_code`, `full_name`, `department_id`,
`department_name`, `work_date`, `status`, `late_minutes`, `worked_minutes`.

#### `GET /reports/absenteeism` → `Page[AbsenteeismPublic]`

Query: `start`, `end`, `group_by` (`department` | `employee`, default
`department`), `department_id`, `limit`, `offset`.

`AbsenteeismPublic`: `group_id`, `group_label`, `expected_days`, `absent_days`,
`on_leave_days`, `late_days`, `attended_days`, `absence_rate`,
`attendance_rate`.

```bash
curl -G http://localhost:8000/api/v1/reports/absenteeism \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  --data-urlencode 'start=2026-10-01' \
  --data-urlencode 'end=2026-10-31' \
  --data-urlencode 'group_by=department'
```

---

## Chat

#### `POST /chat` — *authenticated* → **200**

Body (`ChatRequest`):

| Field | Type | Rules |
| --- | --- | --- |
| `message` | str | 1–2000 characters |
| `history` | `ChatTurn[]` | ≤10 turns, each `{role, content}` |

`ChatTurn.role` accepts **only** `user` or `assistant`. A `system` turn would
let a client rewrite the assistant's rules and a `tool` turn would let one
fabricate a result that was never computed, so both are a **422**.

Conversation state lives with the client — there is no stored-conversation
model. History is replayed under the same authorisation as a fresh question, so
a tampered history cannot widen what the answer may contain.

Response (`ChatResponse`):

```json
{
  "answer": "You were late twice in the last 30 days.",
  "grounded": true,
  "tools_used": [
    {
      "name": "attendance_summary",
      "arguments": { "start_date": "2026-09-24", "end_date": "2026-10-24" },
      "ok": true
    }
  ]
}
```

`grounded` is **false** when no tool backed the answer — a refusal, a greeting,
or a model that ignored its instructions. Clients should present those
differently from a figure that came out of the database.

The assistant may call six read-only tools, defined in `app/ai/tools.py`:
`attendance_summary`, `late_arrivals`, `absences`, `attendance_trend`,
`leave_requests` and `shift_info`. Each takes a validated Pydantic argument
model — typically `start_date`, `end_date` and an optional `employee_code` —
and each delegates its arithmetic to `app/services/reports.py`.

Authorisation is enforced **before** any query runs, in `app/ai/tools.py`: a
non-admin is pinned to their own records, and a request for someone else's data
raises rather than being silently rewritten. The model never sees SQL and never
receives raw rows for anyone the caller may not read.

**503** when the provider is unavailable or no API key is configured. Nothing
about the provider, the key or the upstream error is echoed back. The attempt
is still recorded in the audit trail as a `query` event with the question's
*length* — never its text, because attendance questions carry personal detail
and an audit trail is read by people who need to know that a query happened,
not what was confided in it.

---

## Known limitations

- **Leave has no REST endpoint.** `LeaveRequest` is modelled, constrained and
  honoured by the reporting layer (approved leave suppresses an absence), but
  no route creates, lists, approves or rejects one. Leave rows exist only via
  the seeder or direct SQL.
- **`/docs`, `/redoc` and `/openapi.json` are always served**, including when
  `APP_ENV=prod`. They expose the full route surface and schemas (not data). If
  that is not wanted, block the paths at the reverse proxy or make the
  `docs_url` arguments in `create_app()` conditional.
- **There is no admin endpoint to correct an attendance record**, to change a
  user's role, or to delete a user. Those are out-of-band operations today.
- **No rate limiting** is applied to `/auth/login` or `/chat` at the
  application layer.
