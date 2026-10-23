"""Response schemas for the audit trail.

Read-only by design: there is no create, update or delete schema here, because
no endpoint accepts one. The trail is written as a side effect of the action it
describes, never by a client.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import AuditAction


class AuditLogPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: UUID | None
    action: AuditAction
    entity_type: str
    entity_id: UUID | None
    summary: str | None
    changes: dict | None
    ip_address: str | None

    @field_validator("ip_address", mode="before")
    @classmethod
    def _stringify_inet(cls, v: object) -> str | None:
        """The INET column comes back as an IPv4Address/IPv6Address object."""
        return None if v is None else str(v)

    # Stored as timestamptz and serialised in UTC.
    created_at: datetime
