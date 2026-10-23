"""Schemas shared across feature modules."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A single page of results.

    ``total`` is the count matching the filters, not the page size, so a client
    can tell whether more pages exist without probing for an empty one.
    """

    items: list[T]
    total: int
    limit: int
    offset: int
