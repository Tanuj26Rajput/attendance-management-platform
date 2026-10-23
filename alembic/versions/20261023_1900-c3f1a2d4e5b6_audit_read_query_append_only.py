"""Add read/query audit actions and make audit_logs append-only.

The trigger is the point of this migration. Not exposing an update or delete
endpoint makes the audit trail append-only by convention; a trigger makes it
append-only in fact, including against a direct psql session or a future bug in
our own code. An audit trail that the audited party can quietly edit is not
evidence of anything.

Revision ID: c3f1a2d4e5b6
Revises: 5de20afe1b50
Create Date: 2026-10-23 19:00:00.000000
"""

from __future__ import annotations

from alembic import op

revision = "c3f1a2d4e5b6"
down_revision = "5de20afe1b50"
branch_labels = None
depends_on = None

_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_logs_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'audit_logs is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is allowed inside a transaction on PostgreSQL 12+
    # provided the new label is not *used* before commit. Nothing here uses it.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'read'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'query'")

    op.execute(_GUARD_FUNCTION)
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_append_only
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
    # Enum labels are deliberately left in place: PostgreSQL cannot drop a
    # label, and recreating the type would require rewriting every row that
    # references it.
