# Deployment

> Nothing here has been deployed. This describes what the committed
> `Dockerfile`, `docker-compose.yml` and `docker-compose.prod.yml` define.

## Images

One multi-stage `Dockerfile` produces two final targets off a shared runtime
layer.

```mermaid
flowchart LR
    base["<b>base</b><br/>python slim"] --> deps["<b>deps</b><br/>requirements.txt"]
    deps --> runtime["<b>runtime</b><br/>app code<br/>non-root user <i>appuser</i> (uid 1000)"]
    runtime --> api["<b>api</b><br/>EXPOSE 8000<br/>HEALTHCHECK /api/v1/health<br/>CMD uvicorn"]
    runtime --> dash["<b>dashboard</b><br/>EXPOSE 8501<br/>HEALTHCHECK /_stcore/health<br/>CMD streamlit"]
```

`api` and `dashboard` are separate stages rather than one image run two ways
because `HEALTHCHECK` is baked into the image: sharing a stage would give the
dashboard the API's probe, which it can never pass.

## Development stack — `docker-compose.yml`

```mermaid
flowchart TB
    dev["Developer<br/>browser"]

    subgraph host["Developer machine"]
        subgraph net["compose network"]
            capi["<b>amp-api</b><br/>target: api<br/>uvicorn --reload<br/>0.0.0.0:8000"]
            cdash["<b>amp-dashboard</b><br/>target: dashboard<br/>0.0.0.0:8501"]
        end
        src["./app, ./alembic, ./dashboard<br/><i>bind-mounted read-only</i>"]
        envfile[".env<br/><i>env_file, never copied into the image</i>"]
    end

    supa[("Supabase PostgreSQL<br/><i>remote, managed</i>")]
    or["OpenRouter API"]

    dev -->|":8501"| cdash
    dev -->|":8000/docs"| capi
    cdash -->|"API_BASE_URL=http://api:8000"| capi
    capi --> supa
    capi --> or
    src -.->|live reload| capi
    src -.-> cdash
    envfile -.-> capi
    envfile -.-> cdash
    cdash -.->|"depends_on:<br/>service_healthy"| capi
```

Bind mounts and `--reload` are exactly what make this unsuitable for a deploy.
Migrations are never run from here.

## Production stack — `docker-compose.prod.yml`

`docker-compose.prod.yml` is **standalone, not an override**: Compose merges
volume lists by target rather than replacing them, so layering it over the dev
file would keep the bind mounts and `--reload`.

```mermaid
flowchart TB
    users["Users<br/>employees · administrators"]

    subgraph hostbox["Deployment host"]
        proxy["<b>Reverse proxy</b><br/><i>not provided by this repo</i><br/>TLS termination<br/>websocket upgrade<br/>sticky sessions if >1 dashboard"]

        subgraph compose["docker compose -f docker-compose.prod.yml"]
            papi["<b>amp-api:IMAGE_TAG</b><br/>127.0.0.1:8000<br/>--proxy-headers<br/>--timeout-graceful-shutdown 30<br/>stop_grace_period 35s"]
            pdash["<b>amp-dashboard:IMAGE_TAG</b><br/>127.0.0.1:8501"]
            pmig(["<b>migrate</b><br/>profile: migrate<br/>alembic upgrade head<br/><i>one-shot, run --rm</i>"])
        end

        logs["json-file log driver<br/><i>size + file rotation</i>"]
    end

    supa[("Supabase PostgreSQL")]
    or["OpenRouter API"]

    users -->|HTTPS 443| proxy
    proxy -->|HTTP| papi
    proxy -->|"HTTP + ws"| pdash
    pdash -->|API_BASE_URL| papi
    papi --> supa
    pmig -.->|"once per deploy,<br/>before rolling the API"| supa
    papi --> or
    papi -.-> logs
    pdash -.-> logs

    style pmig stroke-dasharray: 5 5
```

### Why the pieces sit where they do

| Decision | Reason |
| --- | --- |
| Both services bind `127.0.0.1` | uvicorn and Streamlit are never exposed directly; a proxy terminates TLS |
| `migrate` is a profile-gated one-shot | `up` must never migrate: with more than one API replica, every replica would race `alembic upgrade head` against the same database |
| `stop_grace_period: 35s` > `--timeout-graceful-shutdown 30` | the container is not killed while uvicorn is still draining |
| `FORWARDED_ALLOW_IPS` set to the proxy | until it is, uvicorn trusts only `127.0.0.1`, so a forged `X-Forwarded-For` cannot put a false client IP in the audit trail |
| json-file rotation | an unbounded container log fills the host disk |
| `.env` via `env_file`, never `COPY` | secrets stay out of image layers; `.dockerignore` excludes `.env`, `.git`, `.streamlit`, `*.log` |

### Deployment sequence

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant D as Docker
    participant M as migrate (one-shot)
    participant PG as Supabase PostgreSQL
    participant A as api
    participant S as dashboard

    Op->>D: compose -f prod build
    D-->>Op: images amp-api, amp-dashboard tagged

    Op->>D: compose -f prod --profile migrate run --rm migrate
    D->>M: alembic upgrade head
    M->>PG: apply pending revisions
    PG-->>M: ok
    M-->>Op: exit 0
    Note over Op,M: Wait for exit 0 before continuing.<br/>A non-zero exit stops the deploy here.

    Op->>D: compose -f prod up -d
    D->>A: start
    A->>PG: connection pool
    D->>S: start (depends_on api healthy)
    S->>A: GET /api/v1/health

    Op->>A: curl /api/v1/health/ready
    A->>PG: SELECT 1
    A-->>Op: {"status":"ok","database":"reachable"}
```

## Scaling notes

- **The API is stateless** apart from its connection pool, so replicas scale
  horizontally. Mind Supabase's connection ceiling: use the transaction pooler
  (port 6543) with `DB_USE_PGBOUNCER=true`, which disables asyncpg's
  prepared-statement cache.
- **The dashboard is not stateless.** It keeps the auth token in Streamlit's
  per-session state, so more than one replica needs **sticky sessions** — a
  user moved between replicas is signed out.
- **Alembic should use the direct connection (port 5432)**, not the pooler.
