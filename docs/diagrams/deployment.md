# Deployment

> This describes the committed `Dockerfile` and `docker-compose*.yml` files.
> Nothing here has actually been deployed to a live server.

```mermaid
flowchart LR
    user["User"] -->|HTTPS| api["Docker: API service<br/>(FastAPI, :8000)"]
    user -->|HTTPS| dash["Docker: Dashboard service<br/>(Streamlit, :8501)"]
    dash -->|"REST + JWT"| api
    api --> db[("Supabase PostgreSQL<br/>managed, remote")]
    api --> or["OpenRouter API<br/>(external service)"]

    style db stroke-dasharray: 0
    style or stroke-dasharray: 5 5
```

**Configured today:** the API and dashboard Docker images/services, and the
connection to Supabase PostgreSQL and OpenRouter.

**Not configured / not yet deployed:** a reverse proxy, TLS certificates, and
any real server or cloud hosting. `docker-compose.prod.yml` describes a
production-style stack but has not been run against a live host.

Migrations run as a one-off step (`--profile migrate run --rm migrate`)
before starting the services, so the API never races itself applying schema
changes.
