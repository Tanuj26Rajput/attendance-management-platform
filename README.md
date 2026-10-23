# Attendance Management Platform

AI-powered attendance management: a FastAPI backend on Supabase PostgreSQL, with a
Streamlit dashboard for operators.

## What is implemented

- **Authentication** — registration (employee only, by construction), login,
  logout with token revocation, `GET /auth/me`. Argon2id password hashing,
  JWT bearer tokens.
- **Role-based access control** — two roles, `employee` and `admin`, declared
  as a dependency in one module so a route cannot be written unprotected.
- **Attendance** — punch in / punch out, today's record, own history. Late,
  half-day, early-leave and overtime are computed by a pure rules engine that
  understands grace periods, per-shift timezones and overnight boundaries.
- **Administration** — departments, shifts and employee-shift assignments
  (create, list, update, deactivate / end), with database-enforced protection
  against overlapping assignments.
- **Reporting and analytics** — daily summaries, trends by day/week/month,
  late arrivals, derived absences and absenteeism by department or employee.
  Every figure is computed deterministically in Python/SQL.
- **AI assistant** — a grounded chatbot over six read-only tools. The model
  never computes a number, never sees SQL, and authorisation is enforced
  before retrieval.
- **Audit trail** — append-only, enforced by a database trigger, admin-only to
  read. Report reads and chat queries are audited too.
- **PII redaction** — applied to every log record, including uvicorn's access
  log.
- **Streamlit dashboard** — login, role-based navigation, employee and admin
  views, charts and the assistant. It calls the API over HTTP and never
  touches the database.
- **Demo-data seeder** — repeatable, idempotent, development-gated.

Not implemented: **leave has no REST endpoint** (the model exists and
reporting honours approved leave, but nothing exposes it). See
[Known limitations](#known-limitations).

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/api.md`](docs/api.md) | every route, auth requirement, schema, pagination and error |
| [`docs/tradeoffs.md`](docs/tradeoffs.md) | what was chosen, what it costs, what was given up |
| [`docs/demo-credentials.md`](docs/demo-credentials.md) | demo sign-in details — **development only** |
| [`docs/diagrams/architecture.md`](docs/diagrams/architecture.md) | system context, request path, layering |
| [`docs/diagrams/deployment.md`](docs/diagrams/deployment.md) | images, dev and prod stacks, deploy sequence |
| [`docs/diagrams/components.md`](docs/diagrams/components.md) | modules, RBAC, analytics, chatbot, audit |
| [`docs/diagrams/er-diagram.md`](docs/diagrams/er-diagram.md) | tables, foreign keys, constraints |
| [`docs/diagrams/sequence-diagrams.md`](docs/diagrams/sequence-diagrams.md) | login, punching, reporting, admin, chat, audit |

## Stack

| Layer      | Choice                                           |
| ---------- | ------------------------------------------------ |
| API        | FastAPI + Uvicorn                                |
| Database   | Supabase PostgreSQL via SQLAlchemy 2 (async/asyncpg) |
| Migrations | Alembic (async env)                              |
| Dashboard  | Streamlit                                        |
| Tests      | pytest + pytest-asyncio + httpx ASGI transport   |
| Packaging  | Docker (multi-stage) + Docker Compose            |
| AI         | OpenRouter (`stepfun/step-3.7-flash`) via the `openai` SDK |

## Layout

```
app/
  main.py            FastAPI app factory + lifespan
  core/              settings, logging, security (Argon2 + JWT)
  db/                declarative base, mixins, async engine & session
  models/            ORM models; every module is re-exported in __init__ for Alembic
  schemas/           auth, attendance, admin, reports, chat, audit
  core/pii.py        masking and redaction for logs and LLM-bound text
  api/
    deps.py          all authn/authz lives here (CurrentUser, CurrentAdmin)
    v1/router.py     aggregates v1 routes
    v1/routes/       health, auth, attendance, admin, reports, chat
  services/          auth, attendance, attendance_rules, directory, reports, chat
  ai/                OpenRouter client, tool definitions, tool execution
  seed/              demo-data seeder (dev only; never run on startup)
alembic/             migration environment + versions/
dashboard/
  app.py             entrypoint: login gate and role-based navigation
  api_client.py      the only route to data; one method per endpoint
  session.py         token storage, expiry, sign-in/out
  components.py      pagination, filters, tables, empty states
  charts.py          Plotly figure builders (pure: rows in, figure out)
  theme.py           validated chart palette, light and dark
  views/             one module per page
tests/               unit tests (no DB) + integration tests (marked)
```

The dashboard never imports the ORM — it only calls the API. That keeps a single
enforcement point for auth and validation.

## AI provider

The platform calls **OpenRouter**, which exposes an OpenAI-compatible API — so the
official `openai` SDK is used unchanged, pointed at a different `base_url`. Get a
key from <https://openrouter.ai/keys> and set `OPENROUTER_API_KEY`.

The default model is `stepfun/step-3.7-flash` (262k context; ~$0.20/M input,
~$1.15/M output). Two of its traits shape the code:

- **Reasoning is mandatory.** It cannot be switched off, only dialled
  `low` / `medium` / `high` via `OPENROUTER_REASONING_EFFORT`. Reasoning tokens
  bill at the completion rate, so use `low` for simple extraction and save
  `high` for genuine analysis.
- **It is multimodal** (text + image + video in), which leaves the door open for
  photo-based check-in verification later.

It supports tool calling and structured outputs, so attendance queries can be
grounded in real database results rather than free-text guesses.

Swapping models is a one-line `.env` change — any OpenRouter slug works.

## Data model

Seven tables, all with UUID primary keys and `created_at` / `updated_at`
timestamps (except `audit_logs`, which is append-only and has no `updated_at`).

| Table | Purpose | Key constraints |
| --- | --- | --- |
| `users` | Employees and admins in one table, split by `role` | unique `email`, `employee_code`; self-FK `manager_id` |
| `departments` | Org units | unique `name`, `code`; `head_id` -> users |
| `shifts` | Named working patterns | unique `name`, `code`; `working_days` as ISO weekday array |
| `employee_shift_assignments` | Which shift an employee works, over a date range | unique `(user_id, effective_from)`; one open-ended primary shift per user |
| `attendance_records` | One row per employee per day | unique `(user_id, work_date)`; checkout >= checkin |
| `leave_requests` | Leave and its approval trail | `end_date >= start_date`; decided rows must name a reviewer |
| `audit_logs` | Append-only activity trail | JSONB `changes`, indexed by entity and actor |

Notes on a few deliberate choices:

- **Attendance is daily-grained**, not one row per punch. That is what makes
  `(user_id, work_date)` uniquely enforceable; multiple punches fold into
  first-in / last-out.
- **`attendance_records.shift_id` is denormalised** from the assignment table on
  purpose, so reassigning someone's shift later does not retroactively change
  whether they were late last month.
- **`departments.head_id` and `users.department_id` reference each other**, so
  the former is declared `use_alter=True` and its relationship `post_update=True`
  to break the dependency cycle at DDL and flush time.
- **Delete behaviour is explicit on every FK**: `CASCADE` for rows meaningless
  without their user, `SET NULL` for references that must survive a deletion
  (audit actors, approvers), and `RESTRICT` on `shifts` so a shift that has been
  worked cannot vanish.

Tables are created by Alembic migrations only — nothing calls `create_all()` at
startup.

## Authentication and attendance

Passwords are hashed with **Argon2id** (`argon2-cffi` directly, not via passlib).
Access is granted by a signed JWT carrying `sub`, `role` and a `jti`.

| Endpoint | Who |
| --- | --- |
| `POST /api/v1/auth/register` | public; **always** creates an employee |
| `POST /api/v1/auth/login` | public |
| `POST /api/v1/auth/logout` | authenticated; revokes the presented token |
| `GET /api/v1/auth/me` | authenticated |
| `POST /api/v1/attendance/punch-in` | employee (own record only) |
| `POST /api/v1/attendance/punch-out` | employee (own record only) |
| `GET /api/v1/attendance/me` | employee (today) |
| `GET /api/v1/attendance/me/history` | employee (own history) |
| `POST /api/v1/chat` | authenticated (pinned to own data unless admin) |
| `GET /api/v1/admin/users` | admin only |
| `/api/v1/admin/departments*` | admin only |
| `/api/v1/admin/shifts*` | admin only |
| `/api/v1/admin/assignments*` | admin only |
| `GET /api/v1/admin/audit-logs` | admin only (read-only; no write endpoint) |
| `GET /api/v1/reports/*` | admin only |
| `GET /api/v1/health`, `/health/ready` | public |

Full request/response detail is in **[`docs/api.md`](docs/api.md)**.

Authorisation lives in one place, `app/api/deps.py`: a route declares
`CurrentUser` or `CurrentAdmin` rather than calling a checker, so a protected
route cannot be written by forgetting to protect it.

**Registration can never grant admin**, enforced twice over: `UserRegister` has
no `role` field and sets `extra="forbid"`, so a smuggled `role` is a 422; and
`register_user()` hard-codes `UserRole.EMPLOYEE` without reading the input at
all, so the rule survives the schema being loosened later. Admins are created
out of band.

**Logout works because tokens carry a `jti`.** A JWT is self-contained and
cannot be withdrawn, so logout records the id in `revoked_tokens` and every
authenticated request checks it. `get_current_user` reloads the user from the
database rather than trusting the token's claims, so deactivating someone takes
effect on their next request instead of whenever their token happens to expire.

**Attendance timestamps are stored in UTC.** Shift `start_time`/`end_time` are
naive wall-clock values read in the shift's own `timezone`, which is
authoritative: a shift is a property of the workplace, not of whoever is
rostered onto it, so two employees on the same shift are judged late at the
same instant regardless of where they live. `User.timezone` is only a fallback
for an employee with no shift assigned. All rule logic lives in
`app/services/attendance_rules.py`, which is pure and has no database
dependency, so timezone, grace-period and overnight-boundary behaviour is
unit-tested without Postgres.

Duplicate punches are prevented twice: by an explicit check, and by the
`uq_attendance_records_user_work_date` constraint, which is what actually makes
it safe when two requests race. Punch-out locks the row `FOR UPDATE`.

## Setup

**1. Create a virtual environment**

```bash
python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate
```

**2. Install dependencies**

```bash
pip install -r requirements-dev.txt   # runtime + test/lint tooling
# or, runtime only:
pip install -r requirements.txt
```

**3. Configure the environment**

```bash
cp .env.example .env    # Windows: copy .env.example .env
```

Fill in `DATABASE_URL` from **Supabase Dashboard → Project Settings → Database →
Connection string**. Take the URI and replace the `postgresql://` scheme with
`postgresql+asyncpg://`:

```
postgresql+asyncpg://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres
```

Use the **direct connection (port 5432)** for Alembic. If you point the running API
at the **transaction pooler (port 6543)**, also set `DB_USE_PGBOUNCER=true` —
pgbouncer's transaction mode is incompatible with asyncpg's prepared-statement
cache, and the app disables that cache when the flag is set.

Generate a real JWT secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Environment variables

Every setting is defined in `app/core/config.py` and read from `.env` (or the
real environment, which wins). Use placeholders like the ones below; never
commit a filled-in `.env` — it is excluded by `.gitignore` and `.dockerignore`,
and is mounted at runtime rather than copied into any image.

**Required**

| Variable | Example / placeholder | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:<PASSWORD>@db.<PROJECT_REF>.supabase.co:5432/postgres` | must use the `asyncpg` driver; a sync DSN is rejected at startup |
| `JWT_SECRET_KEY` | `<48-byte-url-safe-secret>` | generate with the command above; the placeholder value is rejected when `APP_ENV=prod` |

**Application**

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_NAME` | `Attendance Management Platform` | |
| `APP_ENV` | `local` | one of `local` `dev` `staging` `prod` `test` |
| `DEBUG` | `true` | |
| `API_V1_PREFIX` | `/api/v1` | |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `text` | `json` emits one object per line for a log shipper |

**Database**

| Variable | Default | Notes |
| --- | --- | --- |
| `DB_ECHO` | `false` | logs every statement; noisy |
| `DB_POOL_SIZE` | `5` | |
| `DB_MAX_OVERFLOW` | `10` | |
| `DB_POOL_PRE_PING` | `true` | |
| `DB_USE_PGBOUNCER` | `false` | set `true` when pointing at Supabase's transaction pooler (port 6543) |

**Supabase project** (optional; unused by the API's data access)

`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` — all default
to unset. The service-role key bypasses row-level security; keep it server-side
and out of anything the dashboard can read.

**Auth and hashing**

| Variable | Default | Notes |
| --- | --- | --- |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | |
| `ARGON2_TIME_COST` | `3` | |
| `ARGON2_MEMORY_COST` | `65536` | |
| `ARGON2_PARALLELISM` | `4` | |

Lowering any Argon2 parameter below its default is rejected unless
`APP_ENV=test`. The test suite needs cheap hashes; production must not get them
by way of one stray environment variable.

**AI provider**

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | unset | without it, `POST /chat` returns 503 and nothing else is affected |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | a pasted `/chat/completions` suffix is trimmed automatically |
| `OPENROUTER_MODEL` | `stepfun/step-3.7-flash` | any OpenRouter slug |
| `OPENROUTER_REASONING_EFFORT` | `medium` | `low` / `medium` / `high` |
| `OPENROUTER_MAX_TOKENS` | `2048` | 256-32768; without a cap the provider reserves the model's full context against your balance |
| `OPENROUTER_TIMEOUT_SECONDS` | `60.0` | |
| `OPENROUTER_MAX_RETRIES` | `2` | |
| `OPENROUTER_SITE_URL`, `OPENROUTER_APP_NAME` | unset | optional attribution |

**CORS and clients**

| Variable | Default | Notes |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:8501,http://localhost:3000` | comma-separated or a JSON array. `*` is **rejected** when `APP_ENV=prod`: the API sends credentials, and Starlette echoes the request's `Origin` back, so a wildcard would let any site read authenticated responses |
| `API_BASE_URL` | `http://localhost:8000` | where the dashboard looks for the API |

Deployment-only, read by uvicorn rather than by the app: `FORWARDED_ALLOW_IPS`
— set it to the reverse proxy's address, or a forged `X-Forwarded-For` can put
a false client IP in the audit trail.

**4. Run the migrations**

```bash
alembic upgrade head        # apply every migration up to the current head
```

That creates all eight tables, the enum types, the `btree_gist` extension, the
assignment-overlap exclusion constraint and the audit append-only trigger.
Nothing calls `create_all()` — migrations are the only way the schema is built.

Other Alembic commands you may need:

```bash
alembic current                                    # which revision is applied
alembic history --verbose                          # the revision graph
alembic revision --autogenerate -m "add thing"     # after changing a model
alembic downgrade -1                               # step back one revision
```

Autogenerate does **not** see exclusion constraints or triggers. The
assignment-overlap `EXCLUDE` and the `audit_logs` trigger are written by hand
in their migration; if you edit them, edit the migration.

**5. Run the services**

```bash
uvicorn app.main:app --reload --port 8000   # API  -> http://localhost:8000/docs
streamlit run dashboard/app.py              # UI   -> http://localhost:8501
```

## Verify the setup

```bash
curl http://localhost:8000/api/v1/health        # liveness, no DB needed
curl http://localhost:8000/api/v1/health/ready  # readiness, runs SELECT 1
pytest                                          # smoke tests
```

`/health` answers without a database; `/health/ready` is the one that proves your
Supabase credentials work.

## Seeding demo data

An optional, repeatable seeder fills an empty database with a believable
organisation so the dashboard and the assistant have something to show.

```bash
make seed                 # or: python -m app.seed --yes
```

It will refuse to run unless **both** guards pass:

- `APP_ENV` is `local`, `dev` or `test` — never `staging` or `prod`; and
- you pass `--yes` (or set `SEED_DEMO_CONFIRM=yes`).

Nothing calls it from application startup or from a migration, so it cannot
fire by accident on a deploy. It only ever adds: rows are matched on their
natural keys, so a second run fills gaps instead of duplicating, and it never
deletes or resets data belonging to anyone outside the demo roster. The whole
dataset is written in one transaction, so a partial failure leaves the database
as it was found.

**Sign-in details are in [`docs/demo-credentials.md`](docs/demo-credentials.md)
— development use only.** Start with **`ada.whitfield@example.com`**, the only
administrator; the rest are employees. Note that the platform has no manager
role, so the two "department managers" are seeded as department heads with a
real reporting line and no extra privileges — the credentials file explains
why.

What you get: 4 departments, 4 shifts (including an overnight shift and a
weekend shift, in three timezones with different grace periods), 11 people, one
open-ended shift assignment each, ~30 days of attendance, approved and pending
leave, one administrative attendance correction, and an audit trail.

Attendance is not hand-written. The seeder generates punch times and runs them
through the real rules engine in `app/services/attendance_rules.py`, so every
stored status, `late_minutes` and `worked_minutes` is what the application
itself would have computed. Absences are gaps rather than rows, because that is
how reporting defines them: an expected working day with no record and no
approved leave.

### Suggested demo walkthrough

1. **Live punch.** Sign in as `tomas.lindqvist@example.com` — he has no record
   for today, so **Punch in** works immediately. Then sign in as
   `hana.sugiyama@example.com`, who is already checked in, and **Punch out**.
2. **Overnight and timezones.** `rafael.monteiro@example.com` works 22:00–06:00
   UTC; his history shows a shift that belongs to the day it *starts* on.
   `gabriel.osei@example.com` works Saturdays and Sundays only.
3. **Leave.** `oliver.brennan@example.com` has a pending request;
   `chidinma.okonkwo@example.com` has an approved one, and the days it covers
   carry no attendance record and are not counted as absences.
4. **Admin.** As Ada, walk the directory, the reports and the analytics charts,
   then open the audit trail — the attendance correction is there with its
   before/after.
5. **Assistant.** As Ada, ask *"How many employees were absent last Monday?"*
   and *"Show late arrivals for the Sales department this month."* As any
   employee, ask *"Summarize my attendance for the last 30 days."* As Ada,
   *"Which employees have pending leave requests?"* Then ask an employee
   account about somebody else's attendance and watch it decline — isolation is
   enforced before the query runs, not by the model.

## API documentation

With the API running:

| URL | What |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI — interactive, with a working **Authorize** button for a bearer token |
| <http://localhost:8000/redoc> | ReDoc — a readable reference |
| <http://localhost:8000/openapi.json> | the raw OpenAPI schema |

All three are configured unconditionally in `create_app()`, so they are served
in **every** environment, including production. They expose the route surface
and schemas, not data. Block the paths at the reverse proxy if that is not
wanted.

A hand-written reference, including error codes and example requests, is in
[`docs/api.md`](docs/api.md).

## Tests

Run these yourself — nothing here runs them for you.

```bash
pytest                      # unit + API smoke tests (no database required)
pytest -m integration       # tests that need a live database
pytest -m ""                # everything, integration included
pytest --cov=app            # with coverage
pytest tests/test_dashboard.py -q          # one file
make lint                   # ruff check . && mypy app
make format                 # ruff format . && ruff check --fix .
```

Tests marked `integration` are deselected by default via `addopts` in
`pyproject.toml`, which is why `-m ""` is needed to include them. Integration
tests need a reachable `DATABASE_URL` and run inside a savepoint that is rolled
back, so they do not leave rows behind — except `tests/test_concurrency.py`,
which commits on purpose so separate connections can genuinely race, and cleans
up after itself.

`tests/conftest.py` sets `APP_ENV=test` and lowers the Argon2 work factor
itself, so the suite does not pay full hashing cost per created user. That is
the only environment in which weakened parameters are accepted — a settings
validator rejects them everywhere else.

## Docker

```bash
docker compose up --build   # API on :8000, dashboard on :8501
docker compose down -v
```

Compose reads `.env`, so create it first. `docker-compose.yml` is the
**development** stack: it bind-mounts the source read-only and runs uvicorn with
`--reload`.

The image is multi-stage and builds two final targets off one shared runtime
layer — `api` and `dashboard` — each with its own `EXPOSE`, `CMD` and
`HEALTHCHECK`. They are separate stages rather than one image run two ways
because a `HEALTHCHECK` is baked into the image: sharing a stage gives the
dashboard the API's probe, which it can never pass.

## Production deployment

`docker-compose.prod.yml` is **standalone, not an override** — Compose merges
volume lists by target rather than replacing them, so layering it would keep
the dev bind mounts and `--reload`.

```bash
# 1. Build
docker compose -f docker-compose.prod.yml build

# 2. Migrate — once per deploy, before rolling the API. Wait for exit 0.
docker compose -f docker-compose.prod.yml --profile migrate run --rm migrate

# 3. Start
docker compose -f docker-compose.prod.yml up -d

# 4. Check
docker compose -f docker-compose.prod.yml ps
curl http://127.0.0.1:8000/api/v1/health/ready
```

Migrations are a **profile-gated one-shot service**, so `up` never runs them.
That is deliberate: with more than one API replica, migrating on startup means
every replica races `alembic upgrade head` against the same database.

Both services bind to `127.0.0.1`. Terminate TLS in a reverse proxy on the host
and forward to them; do not expose uvicorn directly. The proxy must forward
websocket upgrades for the dashboard, and if you run more than one dashboard
replica it needs **sticky sessions** — the dashboard keeps its auth token in
Streamlit's per-session state, so a user moved between replicas is signed out.

Set `FORWARDED_ALLOW_IPS` to the proxy's address once one is in front of the
API. Until then uvicorn trusts only `127.0.0.1`, so a forged `X-Forwarded-For`
cannot put a false client IP in the audit trail.

Secrets stay out of the image: `.dockerignore` excludes `.env` (and any
`.streamlit/secrets.toml`), and nothing is copied in — Compose reads `.env` on
the host and passes it as runtime environment.

## Common commands

A `Makefile` wraps the above: `make install`, `make run`, `make dashboard`,
`make test`, `make lint`, `make format`, `make migrate`,
`make revision m="add employees"`, `make seed`, `make docker-up`.

## Known limitations

- **Leave has no REST endpoint.** `LeaveRequest` is modelled, constrained and
  honoured by reporting — approved leave suppresses an absence — but no route
  creates, lists, approves or rejects one. Rows exist only via the seeder or
  direct SQL. This is the largest feature gap.
- **Two roles only.** There is no manager or team-lead tier, so `/reports/*` is
  all-or-nothing. `users.manager_id` and `departments.head_id` record reporting
  lines and grant no privileges.
- **No endpoint corrects an attendance record, changes a role, or deletes a
  user.** Those are out-of-band operations today.
- **A user with audit history cannot be deleted.** `audit_logs.actor_id` is
  `ON DELETE SET NULL`, but the append-only trigger rejects the `UPDATE` that
  would apply it. Not reachable through the API, and left unresolved on purpose
  — it is a retention-versus-erasure policy decision. See
  [`docs/tradeoffs.md`](docs/tradeoffs.md#audit-retention-versus-the-right-to-erasure).
- **`/docs`, `/redoc` and `/openapi.json` are served in production.**
- **No background scheduler**, so nothing marks holidays or closes stale open
  punches. Absence is derived at read time instead, which is why no job is
  needed.
- **`revoked_tokens` is never pruned** and `audit_logs` has no retention
  policy; both grow without bound.
- **The dashboard caches the signed-in user's role**, so a role changed in the
  database only takes effect after signing out and back in.
- **One known failing test**:
  `tests/test_reports_api.py::test_trend_granularity_changes_the_bucket_count`,
  most likely because `tests/test_concurrency.py` opens committed connections on
  the shared session-scoped engine.
- **The demo seeder has not been run against a live database.**

## Security considerations

What is in place:

- **Argon2id** password hashing at OWASP parameters, floored by a settings
  validator outside `APP_ENV=test`.
- **Identical response and timing** for an unknown email and a wrong password —
  a failed login verifies against a dummy hash so response time does not reveal
  whether an account exists.
- **Registration can never grant admin**, enforced twice: the schema has no
  `role` field and forbids extras, and the service hard-codes
  `UserRole.EMPLOYEE` without reading role input at all.
- **Centralised RBAC.** Routes declare `CurrentUser` or `CurrentAdmin`; the
  tests enumerate privileged routes from the OpenAPI schema, so a new admin
  route is covered the moment it is added.
- **Tokens are revocable.** Logout records the `jti`; every request checks it
  and reloads the user from the database rather than trusting token claims, so
  deactivating an account takes effect immediately.
- **Every write schema sets `extra="forbid"`**, so a typo is a 422 rather than a
  silently ignored field.
- **No raw SQL from user input.** Search terms escape `%`, `_` and
  `\\` and match literally.
- **Secrets never reach logs.** A redacting filter runs on the root handler and
  on uvicorn's loggers, including the access log, which does not propagate to
  root and would otherwise be the one log everybody tails and the one that is
  not scrubbed.
- **The audit trail is append-only in fact**, not by convention: a database
  trigger rejects `UPDATE` and `DELETE`, so neither a future endpoint nor a
  direct `psql` session can rewrite history. Passwords, JWTs, API keys and
  chatbot question text are never recorded.
- **The chatbot cannot be talked into a data leak.** Authorisation runs before
  retrieval: a non-admin is pinned to their own records and a request for
  someone else's raises rather than being silently rewritten. The model never
  sees SQL and never receives rows the caller may not read.
- **Production guards in config**: the placeholder JWT secret and a `*` in
  `CORS_ORIGINS` are both rejected when `APP_ENV=prod`.
- **Containers run as a non-root user** and `.env` is never copied into an
  image.

What is **not** in place:

- No rate limiting or account lockout on `/auth/login` or `/chat`.
- No MFA and no password-reset flow.
- No automated dependency-vulnerability gate in CI.
- Compose `env_file` is not a secrets manager; a real deployment should use
  Docker secrets, a cloud secret store, or orchestrator-injected environment.
- Tool *results* are sent to OpenRouter. They are redacted first and raw rows
  never leave, but an external processor does see attendance data.

## Conventions

- Routers stay thin; logic belongs in `app/services/`.
- Every new model module must be imported in `app/models/__init__.py`, or Alembic
  autogenerate will not see it.
- Constraint naming conventions are set on `Base.metadata` to keep migration
  diffs stable.
- `DATABASE_URL` must use the `postgresql+asyncpg://` scheme. Supabase hands you
  `postgresql://`, so the scheme needs swapping every time you copy it; settings
  validation rejects the sync driver with an explicit message rather than letting
  it fail later as a missing-psycopg2 error.
- **Two things autogenerate gets wrong**, both already handled in the initial
  migration and worth re-checking on every new one:
  `use_alter=True` is silently ignored inside `op.create_table`, so the circular
  `departments.head_id` FK must be added by a separate `op.create_foreign_key`;
  and `downgrade()` drops tables but leaves PostgreSQL ENUM types behind, so each
  one needs an explicit `DROP TYPE`, or the next `upgrade` fails on a duplicate
  type.
- `alembic/env.py` sets `include_object` so autogenerate never proposes dropping
  a table it does not own. Without it, pointing at a database shared with another
  application generates `drop_table()` for every foreign table.
- Secrets live in `.env` only. `.env` is git-ignored; `.env.example` is the
  committed contract and must list every variable the app reads.
