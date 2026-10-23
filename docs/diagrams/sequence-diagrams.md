# Sequence diagrams

Each diagram traces a flow that exists in the code today.

---

## 1. Login

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant S as Streamlit<br/>dashboard
    participant C as api_client.py
    participant R as POST /auth/login
    participant A as services/auth.py
    participant SEC as core/security.py
    participant DB as PostgreSQL

    U->>S: email + password
    S->>C: login(email, password)
    C->>R: POST {email, password}
    R->>A: authenticate(email, password)
    A->>DB: SELECT user WHERE email = lower(:email)

    alt no such user
        DB-->>A: none
        A->>SEC: verify_password(None, password)
        Note over A,SEC: Verifies against a dummy hash<br/>so a failed login costs the same<br/>as a successful one — timing does<br/>not reveal whether an email exists.
        A-->>R: InvalidCredentialsError
        R-->>C: 401 "Incorrect email or password"
    else wrong password
        A->>SEC: verify_password(hash, password)
        SEC-->>A: False
        A-->>R: InvalidCredentialsError
        R-->>C: 401 (identical response)
    else inactive account
        A-->>R: InactiveUserError
        R-->>C: 403 "Inactive user account"
    else success
        SEC-->>A: True
        A->>SEC: create_access_token(sub, role)
        SEC-->>A: jwt, jti, expires_at
        A-->>R: user, token
        R-->>C: 200 {access_token, token_type, expires_in}
        C->>S: store in st.session_state
        S->>C: GET /auth/me
        C-->>S: UserPublic {role, ...}
        S->>U: render pages for that role
    end
```

The dashboard caches the role from `/auth/me` at sign-in, so **a role changed
in the database takes effect only after signing out and back in.**

### Logout

```mermaid
sequenceDiagram
    autonumber
    participant S as Dashboard
    participant R as POST /auth/logout
    participant A as services/auth.py
    participant DB as PostgreSQL

    S->>R: Authorization: Bearer <jwt>
    R->>A: revoke_token(jti, user_id, expires_at)
    A->>DB: INSERT INTO revoked_tokens (jti, ...)
    A-->>R: ok
    R-->>S: 200 {"detail": "Token revoked"}
    S->>S: clear session state
    Note over R,DB: A JWT is self-contained and cannot be<br/>withdrawn, so every authenticated request<br/>checks the jti against this list.
```

---

## 2. Punch in / punch out

```mermaid
sequenceDiagram
    autonumber
    actor E as Employee
    participant S as Dashboard
    participant R as POST /attendance/punch-in
    participant D as deps.py
    participant A as services/attendance.py
    participant RU as attendance_rules.py<br/>(pure)
    participant DB as PostgreSQL

    E->>S: Punch in
    S->>R: Bearer token + {source: "web"}
    R->>D: CurrentUser
    D->>DB: revocation check + reload user
    D-->>R: User

    R->>A: punch_in(user, source, ip, ...)
    A->>DB: SELECT active assignment + shift
    alt no assignment in force
        A-->>R: NoActiveShiftError
        R-->>S: 422 "You have no active shift assignment"
    else assignment found
        A->>RU: evaluate_punch_in(now_utc, ShiftRules, tz)
        RU->>RU: resolve_work_date()<br/><i>overnight: 00:30 belongs to yesterday</i>
        RU->>RU: is_working_day()
        RU->>RU: late = now - (start + grace)
        RU-->>A: work_date, status, late_minutes

        alt not a working day for the shift
            A-->>R: NotAWorkingDayError
            R-->>S: 422
        else
            A->>DB: INSERT attendance_records
            Note over A,DB: uq_attendance_records_user_work_date<br/>is the real duplicate guard. Two racing<br/>requests: one commits, the other gets an<br/>IntegrityError translated to 409.
            alt unique violation
                DB-->>A: IntegrityError
                A-->>R: AlreadyPunchedInError
                R-->>S: 409 "You are already punched in for today"
            else inserted
                A->>DB: audit row (same transaction)
                A->>DB: COMMIT
                R-->>S: 201 AttendanceRecordPublic (is_open: true)
            end
        end
    end
```

```mermaid
sequenceDiagram
    autonumber
    actor E as Employee
    participant R as POST /attendance/punch-out
    participant A as services/attendance.py
    participant RU as attendance_rules.py
    participant DB as PostgreSQL

    E->>R: Bearer token + {notes?}
    R->>A: punch_out(user, ip, notes)
    A->>DB: SELECT open record ... FOR UPDATE
    Note over A,DB: The row lock serialises two concurrent<br/>punch-outs: the second sees the closed row.

    alt no open record
        A->>DB: SELECT most recent completed record
        alt a completed record exists
            A-->>R: AlreadyPunchedOutError
            R-->>E: 409 "You have already punched out for that day"
        else never punched in
            A-->>R: NotPunchedInError
            R-->>E: 409 "You have no open punch-in to close"
        end
    else open record found
        A->>RU: evaluate_punch_out(in, out, work_date, rules, tz, late)
        RU->>RU: worked = gross - break_minutes
        RU->>RU: early_leave / overtime vs scheduled end
        RU->>RU: status = half_day if worked < threshold<br/>else late if late_minutes else present
        RU-->>A: status, worked, early_leave, overtime
        A->>DB: UPDATE record + audit row
        A->>DB: COMMIT
        R-->>E: 200 AttendanceRecordPublic (is_open: false)
    end
```

---

## 3. Admin reporting

```mermaid
sequenceDiagram
    autonumber
    actor AD as Administrator
    participant S as Dashboard<br/>(admin_reports view)
    participant R as GET /reports/*
    participant D as deps.py
    participant RP as services/reports.py
    participant AU as services/audit.py
    participant DB as PostgreSQL

    AD->>S: pick a date range / department
    S->>R: Bearer token + start, end, filters
    R->>D: CurrentAdmin
    alt role != admin
        D-->>R: 403 "Insufficient privileges for this operation"
        R-->>S: 403
    else admin
        R->>RP: build_grid(start, end, department_id?, user_ids?)
        alt range > 366 days
            RP-->>R: InvalidDateRangeError
            R-->>S: 422
        else
            RP->>DB: batched load — users, assignments,<br/>shifts, records, approved leave
            Note over RP,DB: One pass, not per-employee queries.<br/>Expected = assignment covers the day<br/>AND working day for the shift<br/>AND the shift has already ended in its tz.
            DB-->>RP: rows
            RP->>RP: deterministic arithmetic<br/><b>no LLM involved</b>
            RP-->>R: dataclass rows
            R->>AU: record_report_access(report, redact(filters))
            AU->>DB: INSERT audit_logs (action = read)
            Note over R,AU: The filters are recorded, never the results.
            R-->>S: 200 Page[...] {items, total, limit, offset}
            S->>AD: tables + Plotly charts
        end
    end
```

---

## 4. Employee and shift administration

```mermaid
sequenceDiagram
    autonumber
    actor AD as Administrator
    participant S as Dashboard<br/>(admin_directory view)
    participant R as /admin/shifts, /admin/assignments
    participant DIR as services/directory.py
    participant DB as PostgreSQL

    AD->>S: create shift "Night Support" 22:00-06:00
    S->>R: POST /admin/shifts {..., is_overnight: true}
    Note over R: ShiftCreate validates: known IANA timezone,<br/>ISO weekdays 1-7 with no duplicates,<br/>and an unambiguous window —<br/>overnight requires end <= start.
    alt window inconsistent with is_overnight
        R-->>S: 422
    else valid
        R->>DIR: create_shift(payload)
        DIR->>DB: INSERT shifts
        alt duplicate name or code
            DB-->>DIR: IntegrityError
            DIR-->>R: DuplicateError
            R-->>S: 409 (operator wording; no table or<br/>constraint name is echoed)
        else
            DIR->>DB: audit row + COMMIT
            R-->>S: 201 ShiftPublic
        end
    end

    AD->>S: assign an employee to the shift
    S->>R: POST /admin/assignments {user_id, shift_id, effective_from}
    R->>DIR: create_assignment(...)
    DIR->>DB: INSERT employee_shift_assignments
    alt overlaps an existing assignment
        DB-->>DIR: ExclusionViolation<br/>(GiST EXCLUDE on user_id + daterange)
        DIR-->>R: AssignmentConflictError
        R-->>S: 409
    else
        DIR->>DB: audit row + COMMIT
        R-->>S: 201 AssignmentPublic
    end

    AD->>S: retire the shift
    S->>R: POST /admin/shifts/{id}/deactivate
    R->>DIR: deactivate_shift(id)
    DIR->>DB: UPDATE shifts SET is_active = false
    Note over DIR,DB: Deactivated, never deleted. The FK from<br/>assignments is ON DELETE RESTRICT, so a<br/>shift that has been worked cannot vanish<br/>and attendance history stays interpretable.
    R-->>S: 200 ShiftPublic
```

Closing an assignment uses `POST /admin/assignments/{id}/end`. Repointing an
assignment at a different shift is deliberately not offered — closing one and
opening another leaves the history showing what was actually in force.

---

## 5. Chatbot query

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant S as Dashboard<br/>(assistant view)
    participant R as POST /chat
    participant CH as services/chat.py
    participant OR as OpenRouter
    participant T as ai/tools.py
    participant RP as services/reports.py
    participant AU as services/audit.py
    participant DB as PostgreSQL

    U->>S: "How many employees were absent last Monday?"
    S->>R: Bearer token + {message, history[<=10]}
    Note over R: Only user/assistant turns are accepted.<br/>A system turn would let a client rewrite<br/>the rules; a tool turn would let one<br/>fabricate a result never computed.

    R->>CH: answer(user, message, history)
    CH->>OR: chat.completions with tool schemas

    alt provider unreachable / no API key
        OR-->>CH: error or timeout
        CH-->>R: AssistantUnavailableError
        R->>AU: record_chat_query(outcome="unavailable")
        R-->>S: 503 (nothing about the provider<br/>or the key is echoed back)
    else tool call requested
        OR-->>CH: tool_call absences{start_date, end_date}
        CH->>T: execute(ctx, name, raw_args)
        T->>T: validate args (Pydantic)
        T->>T: _resolve_subject(ctx, employee_code)
        alt non-admin asking about someone else
            T-->>CH: NotAuthorizedError
            Note over T: Raised, not silently rewritten to<br/>"your own data" — the refusal is<br/>visible rather than disguised.
        else self, or admin
            T->>RP: build_grid + reports.absences(grid)
            RP->>DB: batched read
            DB-->>RP: rows
            RP-->>T: computed rows
            T->>T: redact() before returning to the model
            T-->>CH: {total, items, definition}
        end
        CH->>OR: tool result
        OR-->>CH: natural-language answer
        CH-->>R: answer, grounded=true, tools_used
        R->>AU: record_chat_query(question_chars, tools, grounded)
        AU->>DB: INSERT audit_logs (action = query)
        Note over R,AU: The question text is deliberately absent.<br/>What is kept — which tools ran and whether<br/>the answer was grounded — is what makes<br/>an answer reconstructible later.
        R-->>S: 200 {answer, grounded, tools_used}
        S->>U: answer + which lookups backed it
    end
```

The model never sees SQL and never receives rows the caller may not read.
`grounded: false` means no tool backed the answer — a refusal, a greeting, or a
model that ignored its instructions — and the dashboard presents those
differently from a figure that came out of the database.

---

## 6. Audit logging

Audit rows are not written by a separate call after the fact. They are staged
in the **same transaction** as the change they describe.

```mermaid
sequenceDiagram
    autonumber
    participant R as Any route
    participant SV as Service
    participant AU as services/audit.py
    participant DB as PostgreSQL

    R->>SV: perform the action
    SV->>DB: INSERT / UPDATE the domain row
    SV->>AU: record_audit(actor_id, action, entity, changes)
    AU->>DB: session.add(AuditLog)  — staged, not committed

    alt the action fails
        SV->>DB: ROLLBACK
        Note over SV,DB: The audit row rolls back with it. There is<br/>no window in which the trail claims<br/>something that never happened.
    else the action succeeds
        SV->>DB: COMMIT
        Note over SV,DB: Domain change and audit row land together.
    end
```

```mermaid
sequenceDiagram
    autonumber
    actor AD as Administrator
    participant R as GET /admin/audit-logs
    participant D as deps.py
    participant AU as services/audit.py
    participant DB as PostgreSQL
    participant X as Anyone with DB access

    AD->>R: Bearer token + filters
    R->>D: CurrentAdmin
    alt not an admin
        D-->>AD: 403
    else
        R->>AU: list_audit_logs(actor_id?, entity_type?, action?, start?, end?)
        AU->>DB: SELECT ... ORDER BY created_at DESC
        AU-->>R: rows, total
        R-->>AD: 200 Page[AuditLogPublic]
    end

    Note over R: There is deliberately no POST, PATCH<br/>or DELETE counterpart.

    X->>DB: UPDATE audit_logs SET summary = '...'
    DB->>DB: BEFORE UPDATE OR DELETE trigger
    DB-->>X: ERROR insufficient_privilege:<br/>audit_logs is append-only
```

The trigger is why the trail is append-only *in fact* and not merely by
convention: neither a future endpoint nor a direct `psql` session can rewrite
history. The cost of that guarantee is documented in
[`er-diagram.md`](er-diagram.md#known-modelling-conflict).
