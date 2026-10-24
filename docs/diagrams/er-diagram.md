# Entity-relationship diagram

The main tables and their relationships, from `app/models/`.

```mermaid
erDiagram
    USERS ||--o{ ATTENDANCE_RECORDS : "records"
    USERS ||--o{ EMPLOYEE_SHIFT_ASSIGNMENTS : "is assigned"
    USERS ||--o{ LEAVE_REQUESTS : "requests"
    USERS ||--o{ AUDIT_LOGS : "acts"
    DEPARTMENTS ||--o{ USERS : "employs"
    SHIFTS ||--o{ EMPLOYEE_SHIFT_ASSIGNMENTS : "rosters"
    SHIFTS ||--o{ ATTENDANCE_RECORDS : "scores"

    USERS {
        uuid id PK
        string email UK
        string hashed_password
        string full_name
        enum role "employee | admin"
        uuid department_id FK
        uuid manager_id FK
    }

    DEPARTMENTS {
        uuid id PK
        string name UK
        string code UK
        uuid head_id FK "-> users"
    }

    SHIFTS {
        uuid id PK
        string name UK
        string timezone
        time start_time
        time end_time
        bool is_overnight
        int grace_period_minutes
    }

    EMPLOYEE_SHIFT_ASSIGNMENTS {
        uuid id PK
        uuid user_id FK
        uuid shift_id FK
        date effective_from
        date effective_to "nullable"
    }

    ATTENDANCE_RECORDS {
        uuid id PK
        uuid user_id FK
        date work_date
        uuid shift_id FK
        timestamptz check_in_at
        timestamptz check_out_at
        enum status "present|absent|late|half_day|on_leave|holiday|weekend"
    }

    LEAVE_REQUESTS {
        uuid id PK
        uuid user_id FK
        date start_date
        date end_date
        enum status "pending|approved|rejected|cancelled"
    }

    AUDIT_LOGS {
        uuid id PK
        uuid actor_id FK "nullable"
        enum action
        string entity_type
        jsonb changes
        timestamptz created_at
    }
```

Notes:

- A user can have at most one active shift assignment at a time — enforced
  by a database constraint, not just application code.
- `attendance_records` has one row per employee per day (not per punch).
- `audit_logs` is append-only: a database trigger blocks `UPDATE`/`DELETE`.
  This is why a user with audit history currently cannot be deleted — see
  [`docs/tradeoffs.md`](../tradeoffs.md) for the open decision.
