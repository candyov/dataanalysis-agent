"""Data Intelligence Agent -- FastAPI 入口"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dia.core.config import settings
from dia.infrastructure.observability.logging import setup_logging

setup_logging(level=logging.DEBUG if settings.APP_DEBUG else logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"startup app={settings.APP_NAME} version={settings.APP_VERSION}")
    os.makedirs(settings.STORAGE_DIR, exist_ok=True)
    os.makedirs(settings.STORAGE_OUTPUT_DIR, exist_ok=True)

    # ── API Key 验证 ──
    if not settings.LLM_API_KEY:
        logger.critical("LLM_API_KEY 未配置! 所有 LLM 调用将失败")

    from dia.infrastructure.persistence.sessions import cleanup_expired
    cleanup_expired()
    from dia.engine.metrics import init_metric_store
    init_metric_store()
    logger.info("Stores initialized")

    yield
    cleanup_expired()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    from dia.api.middleware.trace import TraceMiddleware
    from dia.api.middleware.auth import ApiKeyMiddleware
    app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    from dia.api.v1.chat import router as chat_router
    from dia.api.v1.datasources import router as monitoring_router
    from dia.api.v1.settings import router as settings_router
    from dia.api.v1.models import router as models_router
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(monitoring_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(models_router, prefix="/api/v1")
    app.mount("/output", StaticFiles(directory=settings.STORAGE_OUTPUT_DIR), name="output")
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dia.main:app", host="0.0.0.0", port=8010, reload=settings.APP_DEBUG)
