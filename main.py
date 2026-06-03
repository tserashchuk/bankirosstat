from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.logging_config import configure_logging
from app.routers import api, pages
from app.services.jobs.queue import close_arq_pool, create_arq_pool

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    try:
        _app.state.arq_pool = await create_arq_pool()
    except Exception as exc:
        logger.warning(
            "ARQ pool недоступен (%s). Постановка задач в очередь не будет работать "
            "до старта Redis / воркера.",
            exc,
        )
        _app.state.arq_pool = None
    try:
        yield
    finally:
        await close_arq_pool(_app.state.arq_pool)


app = FastAPI(
    title="Project Hub & AI Reporter",
    description="Автоматическая отчётность по проектам",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages.router)
app.include_router(api.router)
