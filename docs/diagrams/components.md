# Components

Modules that exist in the repository today, and what calls what.

```mermaid
flowchart TB
    subgraph ui["dashboard/ — Streamlit"]
        uapp["app.py<br/><i>login gate, role-based nav</i>"]
        usess["session.py<br/><i>token, expiry, sign-in/out</i>"]
        ucli["api_client.py<br/><i>the only route to data</i>"]
        uviews["views/<br/>login · employee · admin_directory<br/>admin_reports · admin_audit · assistant"]
        ucomp["components.py · charts.py · theme.py"]
    end

    subgraph edge["app/api — HTTP edge"]
        main["main.py<br/><i>app factory, CORS, lifespan</i>"]
        rtr["v1/router.py"]
        deps["deps.py<br/><b>all authn/authz</b><br/>CurrentUser · CurrentAdmin · require_roles"]
        r_health["routes/health.py"]
        r_auth["routes/auth.py"]
        r_att["routes/attendance.py"]
        r_admin["routes/admin.py"]
        r_rep["routes/reports.py"]
        r_chat["routes/chat.py"]
    end

    subgraph sch["app/schemas — validation & projection"]
        s_all["auth · attendance · admin<br/>reports · chat · audit · common.Page"]
    end

    subgraph svc["app/services — business logic"]
        sv_auth["auth.py<br/><i>register, authenticate, revoke</i>"]
        sv_att["attendance.py<br/><i>punch in/out, history</i>"]
        sv_rules["attendance_rules.py<br/><b>pure — no I/O</b>"]
        sv_dir["directory.py<br/><i>departments, shifts, assignments</i>"]
        sv_rep["reports.py<br/><b>all analytics arithmetic</b>"]
        sv_chat["chat.py<br/><i>orchestration + grounding</i>"]
        sv_audit["audit.py<br/><i>append-only trail writer</i>"]
    end

    subgraph ai["app/ai"]
        ai_cli["client.py<br/><i>OpenRouter via openai SDK</i>"]
        ai_tools["tools.py<br/><i>6 read-only tools;<br/>RBAC before retrieval</i>"]
    end

    subgraph core["app/core"]
        c_cfg["config.py<br/><i>Settings, prod guards</i>"]
        c_sec["security.py<br/><i>Argon2id + JWT</i>"]
        c_pii["pii.py<br/><i>redact() + RedactingFilter</i>"]
        c_log["logging.py<br/><i>text/json, filtered</i>"]
    end

    subgraph mdl["app/models — ORM + constraints"]
        m["user · department · shift<br/>attendance · leave · audit · token"]
    end

    subgraph ext["External"]
        pg[("Supabase PostgreSQL")]
        orx["OpenRouter"]
    end

    seed["app/seed/<br/><i>demo data, dev only</i>"]
    alb["alembic/<br/><i>versions/</i>"]

    uviews --> ucomp
    uviews --> usess
    usess --> ucli
    uapp --> uviews
    ucli -->|"HTTPS + Bearer"| rtr

    main --> rtr
    rtr --> r_health & r_auth & r_att & r_admin & r_rep & r_chat
    r_auth & r_att & r_admin & r_rep & r_chat --> deps
    r_auth & r_att & r_admin & r_rep & r_chat --> s_all

    deps --> c_sec
    deps --> sv_auth
    r_auth --> sv_auth
    r_att --> sv_att
    r_admin --> sv_dir
    r_admin --> sv_audit
    r_rep --> sv_rep
    r_rep --> sv_audit
    r_chat --> sv_chat

    sv_att --> sv_rules
    sv_rep --> sv_rules
    sv_chat --> ai_cli
    sv_chat --> ai_tools
    sv_chat --> sv_audit
    ai_tools --> sv_rep
    ai_cli --> c_pii
    ai_cli --> orx

    sv_auth & sv_att & sv_dir & sv_rep & sv_audit --> m
    m --> pg
    sv_auth --> c_sec
    sv_audit --> c_pii
    main --> c_log
    c_log --> c_pii
    main --> c_cfg

    seed --> m
    seed --> sv_rules
    seed --> c_sec
    alb --> pg

    style sv_rules fill:#eef6ff,stroke:#2a78d6
    style deps fill:#fff6e6,stroke:#fab219
    style sv_rep fill:#eef6ff,stroke:#2a78d6
```

## Authentication and RBAC

```mermaid
flowchart LR
    tok["Bearer JWT<br/>sub · role · jti · exp"]
    tok --> gtp["get_token_payload()<br/><i>decode, verify signature + exp</i>"]
    gtp --> gcu["get_current_user()"]
    gcu --> rev{"jti in<br/>revoked_tokens?"}
    rev -->|yes| x1["401 revoked"]
    rev -->|no| load["session.get(User, sub)<br/><i>DB is consulted every request</i>"]
    load --> act{"is_active?"}
    act -->|no| x2["403 inactive"]
    act -->|yes| cu["<b>CurrentUser</b>"]
    cu --> rr["require_roles(ADMIN)"]
    rr --> adm{"role == admin?"}
    adm -->|no| x3["403 insufficient privileges"]
    adm -->|yes| ca["<b>CurrentAdmin</b>"]

    style x1 fill:#fdecec,stroke:#d03b3b
    style x2 fill:#fdecec,stroke:#d03b3b
    style x3 fill:#fdecec,stroke:#d03b3b
```

`UserRole` has exactly two members, `employee` and `admin`. Registration can
never grant admin, enforced twice: `UserRegister` has no `role` field and sets
`extra="forbid"`, and `register_user()` hard-codes `UserRole.EMPLOYEE` without
reading role input at all — so the rule survives the schema being loosened
later.

## Analytics

```mermaid
flowchart LR
    q["Admin request<br/>or chatbot tool call"] --> grid["reports.build_grid()<br/><i>one batched load:<br/>users · assignments · shifts<br/>records · approved leave</i>"]
    grid --> exp{"Expected?"}
    exp -->|"assignment covers the day<br/>AND working day for the shift<br/>AND shift already ended in its tz"| yes["counted"]
    exp -->|otherwise| no["not counted"]
    yes --> calc["Deterministic Python/SQL<br/>daily_summaries · attendance_trend<br/>late_arrivals · absences · absenteeism"]
    calc --> proj["Pydantic projection"]

    style calc fill:#eef6ff,stroke:#2a78d6
```

The grid is built once per request to avoid N+1 queries. Ranges are capped at
**366 days**. **No number is produced by a model.** An absence is derived, not
stored: an expected working day with no attendance record and no approved
leave.

## Chatbot

```mermaid
flowchart TB
    msg["message + client-held history<br/><i>only user/assistant turns accepted</i>"] --> svcchat["services/chat.py"]
    svcchat --> ctx["ToolContext<br/>actor · is_admin"]
    svcchat --> llm["OpenRouter<br/>tool-calling loop"]
    llm -->|"requests a tool"| val["Pydantic args model"]
    val --> rbac{"_resolve_subject()"}
    rbac -->|"non-admin asking<br/>about someone else"| den["NotAuthorizedError<br/><i>raised, not silently rewritten</i>"]
    rbac -->|"self, or admin"| run["reports.* executes"]
    run --> red["redact() before the<br/>result goes back to the model"]
    red --> llm
    llm --> ans["answer + grounded + tools_used"]
    ans --> aud["audit: QUERY event<br/><i>question length, tool names,<br/>grounded — never the text</i>"]

    style den fill:#fdecec,stroke:#d03b3b
    style rbac fill:#fff6e6,stroke:#fab219
```

Authorisation runs **before** retrieval, not after. The model never sees SQL,
never sees rows the caller may not read, and `grounded: false` tells the client
no tool backed the answer.

## Audit logging and PII

```mermaid
flowchart LR
    act["Any auditable action"] --> ra["audit.record_audit()<br/><i>staged in the caller's transaction</i>"]
    ra --> tx{"Transaction<br/>commits?"}
    tx -->|no| gone["Audit row rolls back too —<br/>the trail never claims<br/>something that never happened"]
    tx -->|yes| row["audit_logs row"]
    row --> trg["BEFORE UPDATE OR DELETE trigger<br/>trg_audit_logs_append_only"]
    trg --> blocked["UPDATE/DELETE raise<br/>insufficient_privilege"]
    row --> read["GET /admin/audit-logs<br/><i>admin only; no write endpoint</i>"]

    logs["Any log record"] --> filt["RedactingFilter<br/><i>on root + uvicorn,<br/>incl. uvicorn.access</i>"]
    filt --> out["stdout — text or json"]

    style blocked fill:#fdecec,stroke:#d03b3b
```

`audit_logs` has no `updated_at` because an update column would imply
mutability. Passwords, JWTs, API keys and raw chatbot prompts are never
written. The access log is filtered too — a request line such as
`GET /api/v1/admin/departments?search=jane.doe@acme.com` would otherwise be the
one log everyone tails and the one that is not scrubbed.

## External integrations

| Integration | Module | Configuration | Failure behaviour |
| --- | --- | --- | --- |
| Supabase PostgreSQL | `app/db/session.py` | `DATABASE_URL`, `DB_POOL_*`, `DB_USE_PGBOUNCER` | `/health/ready` fails; requests error |
| OpenRouter | `app/ai/client.py` | `OPENROUTER_API_KEY`, `_BASE_URL`, `_MODEL`, `_TIMEOUT_SECONDS`, `_MAX_RETRIES`, `_MAX_TOKENS` | `POST /chat` returns **503**; nothing else is affected |

The rest of the platform has no runtime dependency on the LLM. If OpenRouter is
down or no key is set, attendance, reporting and administration are unaffected.
