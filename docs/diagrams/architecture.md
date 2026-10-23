# Architecture

## System context

Who and what the platform talks to.

```mermaid
flowchart LR
    employee["Employee<br/><i>punches in/out, reads own history</i>"]
    admin["Administrator<br/><i>directory, reports, audit trail</i>"]

    subgraph platform["Attendance Management Platform"]
        dashboard["Streamlit dashboard<br/>:8501"]
        api["FastAPI API<br/>:8000"]
    end

    db[("Supabase PostgreSQL<br/><i>managed</i>")]
    llm["OpenRouter<br/><i>stepfun/step-3.7-flash</i>"]

    employee -->|HTTPS| dashboard
    admin -->|HTTPS| dashboard
    employee -.->|"HTTPS, direct<br/>API clients"| api
    dashboard -->|"REST + bearer JWT"| api
    api -->|"SQLAlchemy 2 async<br/>asyncpg"| db
    api -->|"OpenAI-compatible<br/>chat completions"| llm
```

**The dashboard holds no database credentials.** It reaches data only through
the API, so authentication, authorisation and validation have exactly one
enforcement point. `dashboard/api_client.py` imports nothing from `app`.

## Request path

Every authenticated request takes the same route through the layers.

```mermaid
flowchart TD
    req["HTTP request"] --> cors["CORSMiddleware<br/><i>explicit origins; '*' rejected in prod</i>"]
    cors --> route["Route handler<br/><i>app/api/v1/routes/</i>"]
    route --> deps["app/api/deps.py<br/>CurrentUser / CurrentAdmin"]

    deps --> d1{"Bearer token<br/>present & valid?"}
    d1 -->|no| e401["401"]
    d1 -->|yes| d2{"jti on the<br/>revocation list?"}
    d2 -->|yes| e401
    d2 -->|no| d3["Reload user from DB<br/><i>claims are never trusted</i>"]
    d3 --> d4{"Account active?"}
    d4 -->|no| e403["403"]
    d4 --> d5{"Role sufficient<br/>for this route?"}
    d5 -->|no| e403
    d5 -->|yes| schema["Pydantic schema<br/><i>extra='forbid'</i>"]

    schema -->|invalid| e422["422"]
    schema --> svc["Service layer<br/><i>app/services/</i>"]
    svc --> rules["Pure rules<br/><i>attendance_rules.py</i><br/>no I/O"]
    svc --> orm["SQLAlchemy models<br/><i>constraints enforce invariants</i>"]
    orm --> pg[("PostgreSQL")]
    svc --> audit["Audit row staged in the<br/>same transaction"]
    audit --> pg
    svc --> resp["Response schema<br/><i>projection, never the ORM object</i>"]
```

## Layering rules

```mermaid
flowchart TD
    A["dashboard/<br/><i>Streamlit views</i>"] -->|HTTP only| B["app/api/v1/routes/<br/><i>thin: validate, call, project</i>"]
    B --> C["app/services/<br/><i>all business logic</i>"]
    C --> D["app/services/attendance_rules.py<br/><i>pure functions, no DB</i>"]
    C --> E["app/models/<br/><i>ORM + database constraints</i>"]
    C --> F["app/ai/<br/><i>OpenRouter client + tools</i>"]
    F --> C
    E --> G[("Supabase PostgreSQL")]

    style D fill:#eef6ff,stroke:#2a78d6
    style A fill:#f6f6f2,stroke:#8a8a82
```

Four rules hold everywhere:

1. **Routers stay thin.** A handler validates input, calls a service and
   projects the result. No arithmetic, no queries.
2. **The rules engine is pure.** `attendance_rules.py` has no database
   dependency, so timezone, grace-period and overnight-boundary behaviour is
   unit-tested without Postgres.
3. **Analytics never pass through the model.** Every number in a report or a
   chatbot answer is computed in `app/services/reports.py`; the LLM only
   phrases what was computed.
4. **Invariants live in the database too.** Unique, check and exclusion
   constraints are what actually make concurrent requests safe — the
   application-level checks are for clear error messages, not for correctness.

## Where state lives

| State | Where | Note |
| --- | --- | --- |
| Users, attendance, shifts, leave, audit | Supabase PostgreSQL | the only durable store |
| Access tokens | client-side; `jti` of revoked ones in `revoked_tokens` | JWTs are stateless by design, revocation is not |
| Dashboard session (token, current user, page) | Streamlit per-session state | lost on restart; not shared between replicas |
| Chat conversation history | the client, replayed on each request | no stored-conversation model exists |
| Schema version | `alembic_version` table | migrations only; nothing calls `create_all()` |
