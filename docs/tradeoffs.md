# Trade-off analysis

## Why FastAPI and PostgreSQL

FastAPI gives request validation, response schemas and auto-generated API
docs from the same code, so the API and its documentation can't drift apart.
Most requests here are I/O-bound (a database round-trip, or a slow LLM call
for the chatbot), which is exactly what an async framework handles well.

PostgreSQL (via Supabase) was chosen because the data model leans on real
database features: unique constraints that prevent double punch-ins under a
race, an exclusion constraint that blocks overlapping shift assignments, and
a trigger that enforces an append-only audit log. Putting these rules in the
database — not just in Python — is what makes them hold up under concurrent
requests.

**Cost:** the database is remote (Supabase), so network latency shapes some
code (e.g. reports load all the needed rows in one batch instead of querying
per employee).

## Why Streamlit

Streamlit gives a working dashboard (tables, filters, charts, forms) with
very little code and no separate frontend build — appropriate for an
assessment project. The trade-off is less control over layout/UX than a
proper frontend framework, and the dashboard's session is tied to one
server process (an auth token lives in Streamlit's session state).

The dashboard talks to the API only over HTTP; it has no direct database
access. That keeps auth and validation enforced in exactly one place.

## Why an LLM explains but doesn't calculate

Every attendance number (totals, rates, trends) is computed with plain
Python/SQL. The AI model is only used to **phrase an answer** using a real
number it was given — it never writes SQL or computes a figure itself.

This matters because attendance data affects pay and performance reviews. A
number that's "usually right" isn't good enough, and an LLM's wrong answer
looks just as confident as a right one. Keeping access checks (who can see
whose data) in the Python code — before the model ever sees anything — also
means authorization doesn't depend on the model behaving as instructed.

## Why JWT/RBAC and audit logging

JWT bearer tokens plus two roles (`employee`, `admin`) keep authorization
simple and centralized: every protected route declares the role it needs, so
a route can't be left accidentally unprotected. Passwords are hashed with
Argon2id (the current recommended algorithm).

Audit logging matters because admins and the chatbot can see other people's
attendance data — recording who looked at what, and when, is itself a
safeguard. The log is append-only (enforced by a database trigger), so it
can't be quietly edited later.

## Main limitations

- **Leave requests have no API** yet — the data model exists and reports
  honour approved leave, but there's no endpoint to create or approve one.
- Only two roles — no manager/team-lead tier, so reports are all-or-nothing.
- No endpoint to correct an attendance record, change a role, or delete a
  user.
- No rate limiting on login or the chatbot.
- `/docs` and `/redoc` are exposed even in production.

## Open decision: audit logs vs. user deletion

This is an unresolved conflict, left as-is on purpose rather than patched:

- The audit log is **append-only** — a database trigger blocks any update or
  delete, so the history of admin actions can't be tampered with.
- But deleting a user is supposed to clear their id from old audit rows
  (`SET NULL`), which is itself an "update" — and the trigger blocks that
  too. **Result: a user who has any audit history cannot currently be
  deleted** through the database.

This isn't reachable through the API today (there's no delete-user
endpoint), so nothing is broken in practice. But it's a real conflict
between "keep an untamperable audit trail" and "support a right-to-erasure
request," and resolving it is a policy decision (e.g. allow anonymising the
actor on delete) rather than a quick code fix. **Left unimplemented
deliberately.**
