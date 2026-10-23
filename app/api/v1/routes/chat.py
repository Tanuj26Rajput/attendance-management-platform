"""The attendance assistant endpoint.

Open to any authenticated user, not just admins: an employee asking "how many
times was I late last month" is answering a question about themselves. The
scoping that makes that safe lives in ``app.ai.tools``, which pins a
non-admin's every query to their own records.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import CurrentUser, SessionDep
from app.core.pii import redact_text
from app.schemas.chat import ChatRequest, ChatResponse, ToolCallPublic
from app.services import audit as audit_service
from app.services import chat as chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="Ask a question about attendance",
)
async def ask(
    payload: ChatRequest,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
) -> ChatResponse:
    ip = request.client.host if request.client else None
    try:
        result = await chat_service.answer(
            session,
            user,
            payload.message,
            history=[turn.model_dump() for turn in payload.history],
        )
    except chat_service.AssistantUnavailableError as exc:
        await audit_service.record_chat_query(
            session,
            user,
            question_chars=len(payload.message),
            tools=[],
            grounded=False,
            outcome="unavailable",
            ip=ip,
        )
        # 503, not 500: the platform is fine, the assistant is not. Nothing
        # about the provider or the key is echoed back.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit_service.record_chat_query(
        session,
        user,
        question_chars=len(payload.message),
        tools=[call.name for call in result.tools_used],
        grounded=result.grounded,
        outcome="answered",
        ip=ip,
    )

    return ChatResponse(
        # A last-resort scrub. Tool results carry no contact details by
        # construction, so this should never fire -- but the model is the one
        # component here whose output we cannot constrain by design.
        answer=redact_text(result.answer),
        grounded=result.grounded,
        tools_used=[
            ToolCallPublic(name=call.name, arguments=call.arguments, ok=call.ok)
            for call in result.tools_used
        ],
    )
