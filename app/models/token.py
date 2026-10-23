"""Access-token revocation list.

A JWT is self-contained, so "logout" cannot invalidate one on its own. Tokens
therefore carry a ``jti`` and logging out records that id here; every
authenticated request checks the list.

Rows are only useful until the token would have expired anyway, so
``expires_at`` lets a periodic job purge them.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDMixin

if TYPE_CHECKING:
    from app.models.user import User


class RevokedToken(UUIDMixin, Base):
    __tablename__ = "revoked_tokens"
    __table_args__ = (Index("ix_revoked_tokens_expires_at", "expires_at"),)

    # The token's jti claim. Unique: revoking twice is a no-op, not an error.
    jti: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True)
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # When the token would have expired regardless; rows past this are purgeable.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="revoked_tokens")

    def __repr__(self) -> str:
        return f"<RevokedToken {self.jti}>"
