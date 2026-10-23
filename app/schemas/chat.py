"""Request/response schemas for the attendance assistant."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_QUESTION_CHARS = 2000
MAX_HISTORY_TURNS = 10


class ChatTurn(BaseModel):
    """One prior turn of the conversation.

    Only ``user`` and ``assistant`` are accepted. A ``system`` turn would let a
    client rewrite the assistant's rules, and a ``tool`` turn would let one
    fabricate a result that was never computed.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    # Conversation state lives with the client: there is no stored-conversation
    # model, and history is replayed under the same authorization as a fresh
    # question, so a tampered history cannot widen what the answer may contain.
    history: list[ChatTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


class ToolCallPublic(BaseModel):
    """What the assistant looked up, so an answer can be checked."""

    name: str
    arguments: dict
    ok: bool


class ChatResponse(BaseModel):
    answer: str
    # False when no tool backed the answer -- a refusal, a greeting, or a model
    # that ignored its instructions. Clients should present those differently
    # from a figure that came out of the database.
    grounded: bool
    tools_used: list[ToolCallPublic]
