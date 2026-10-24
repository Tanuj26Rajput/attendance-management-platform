# Sequence diagrams

Three core flows, kept short enough to walk through in a demo.

## 1. Employee punch-in / punch-out

```mermaid
sequenceDiagram
    actor E as Employee
    participant S as Dashboard
    participant API as API
    participant DB as Database

    E->>S: Punch in
    S->>API: POST /attendance/punch-in
    API->>DB: check active shift + insert record
    DB-->>API: ok (or 409 if already punched in)
    API-->>S: 201 attendance record

    E->>S: Punch out
    S->>API: POST /attendance/punch-out
    API->>DB: close open record, compute worked time
    API-->>S: 200 updated record
```

Late, half-day and overtime status are computed by a rules engine using the
employee's shift (timezone, grace period, working hours).

## 2. Admin attendance report

```mermaid
sequenceDiagram
    actor A as Admin
    participant S as Dashboard
    participant API as API
    participant DB as Database

    A->>S: Choose date range / department
    S->>API: GET /reports/daily-summaries
    API->>DB: load attendance + shifts + leave
    DB-->>API: rows
    API->>API: compute summary (Python/SQL, no AI)
    API->>DB: write audit log (read access)
    API-->>S: report data
    S->>A: tables + charts
```

## 3. Chatbot question-answer

```mermaid
sequenceDiagram
    actor U as User
    participant S as Dashboard
    participant API as API
    participant LLM as OpenRouter
    participant DB as Database

    U->>S: "How many employees were absent last Monday?"
    S->>API: POST /chat
    API->>LLM: question + available tools
    LLM-->>API: request: call "absences" tool
    API->>API: check the user is allowed to see this data
    API->>DB: run the real report query
    DB-->>API: rows
    API->>LLM: tool result (data)
    LLM-->>API: natural-language answer
    API->>DB: write audit log (query, no question text)
    API-->>S: answer + which data backed it
```

The model never runs SQL or computes numbers itself — it only picks a tool
and phrases the result. Access checks happen before any data is fetched.
