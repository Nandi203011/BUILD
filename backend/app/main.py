"""
Application entrypoint skeleton.

Phase 1 owns ONLY this wiring — no extraction, RAG, RASE, or RuleEngine
logic is implemented here. Later phases mount their routers onto `app`.
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.config import get_settings
from backend.tools.logging_config import get_logger

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Pre-submission advisory system for Indian municipal building regulations.",
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "environment": settings.environment}

    # Later phases: app.include_router(extraction_router)
    # Later phases: app.include_router(compliance_router)
    logger.info("app initialized", extra={"environment": settings.environment})
    return app


app = create_app()
