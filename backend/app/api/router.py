from fastapi import APIRouter

from app.api.routes import admin, ask, auth, health, search

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(ask.router)
api_router.include_router(search.router)
