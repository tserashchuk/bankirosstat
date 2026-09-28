from __future__ import annotations

import asyncio
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

_ARQ_STARTUP_WAIT = 12.0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _app.state.arq_pool = None

    async def connect_pool() -> None:
        delay = 1.0
        while True:
            try:
                _app.state.arq_pool = await create_arq_pool()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "ARQ pool недоступен (%s). Повтор через %.0fс.",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)

    connect_task = asyncio.create_task(connect_pool())
    try:
        await asyncio.wait_for(asyncio.shield(connect_task), timeout=_ARQ_STARTUP_WAIT)
    except asyncio.TimeoutError:
        logger.warning(
            "ARQ pool пока недоступен. Приложение стартует, реконнект в фоне."
        )

    try:
        yield
    finally:
        connect_task.cancel()
        try:
            await connect_task
        except asyncio.CancelledError:
            pass
        await close_arq_pool(_app.state.arq_pool)
        _app.state.arq_pool = None


app = FastAPI(
    title="Project Hub & AI Reporter",
    description="Автоматическая отчётность по проектам",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(pages.router)
app.include_router(api.router)
