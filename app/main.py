"""Construcción de la API interna y la aplicación pública de webhooks."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import chat, documents, health, webhooks
from app.core import security
from app.workers import idle, inbox

security.configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(idle.idle_monitor()), asyncio.create_task(inbox.inbox_worker())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_app(*, public: bool = False) -> FastAPI:
    application = FastAPI(
        title="Webhooks del chatbot" if public else "SMB Chatbot Assistant",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(security.BodyLimitMiddleware)
    application.include_router(health.router)
    application.include_router(webhooks.router)
    if not public:
        application.include_router(chat.router)
        application.include_router(documents.router)
    return application


app = create_app()
public_app = create_app(public=True)
