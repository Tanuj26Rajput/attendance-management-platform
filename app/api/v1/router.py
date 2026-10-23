"""Aggregates every v1 route module into a single router."""

from fastapi import APIRouter

from app.api.v1.routes import admin, attendance, auth, chat, health, reports

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(attendance.router)
api_router.include_router(admin.router)
api_router.include_router(reports.router)
api_router.include_router(chat.router)

# Remaining feature routers (leave) get included here as they are
# implemented.
