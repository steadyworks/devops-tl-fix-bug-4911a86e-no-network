# Configure logging environment
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Awaitable, Callable

import sentry_sdk
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.session.factory import AsyncSessionFactory
from backend.env_loader import EnvLoader
from backend.lib.asset_manager.factory import AssetManagerFactory
from backend.lib.job_manager.base import JobManager
from backend.lib.job_manager.types import JobQueue
from backend.lib.redis.client import RedisClient
from backend.lib.request.context import RequestContext
from backend.lib.supabase.manager import SupabaseManager
from backend.path_manager import PathManager
from backend.route_handler.base import RouteHandler
from backend.route_handler.debug import DebugHandler
from backend.route_handler.page import PageAPIHandler
from backend.route_handler.photobook import PhotobookAPIHandler
from backend.route_handler.user import UserAPIHandler

from .logging_utils import configure_logging_env

configure_logging_env()

sentry_sdk.init(
    dsn=EnvLoader.get("SENTRY_DSN"),
    send_default_pii=True,
    environment=EnvLoader.get("SENTRY_ENVIRONMENT", "development"),
)


class TimelensApp:
    ENABLED_ROUTE_HANDLERS_CLS: list[type[RouteHandler]] = [
        DebugHandler,
        PhotobookAPIHandler,
        PageAPIHandler,
        UserAPIHandler,
    ]

    def __init__(self) -> None:
        self.path_manager = PathManager()
        self.asset_manager = AssetManagerFactory().create()
        self.db_session_factory = AsyncSessionFactory()
        self.supabase_manager = SupabaseManager()
        self.redis_client = RedisClient()
        self.job_manager = JobManager(self.redis_client, JobQueue.MAIN_TASK_QUEUE)

        self.app: FastAPI = FastAPI(lifespan=self.lifespan)
        self.app.middleware("http")(self._attach_request_context)

        for route_handler_cls in TimelensApp.ENABLED_ROUTE_HANDLERS_CLS:
            self.app.include_router(route_handler_cls(self).get_router())

        self.app.mount(
            "/assets",  # <- this goes first
            StaticFiles(directory=PathManager().get_assets_root()),
            name="assets",
        )

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI) -> AsyncGenerator[None, None]:
        print("Server initializing...")
        print("Server initialize complete...")
        yield
        print("Server cleaning up...")
        await self.db_session_factory.engine().dispose()
        print("Server cleanup complete...")

    @asynccontextmanager
    async def new_db_session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self.db_session_factory.new_session() as session:
            yield session

    async def _attach_request_context(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not request.url.path.startswith("/api"):
            return await call_next(request)

        async with self.new_db_session() as db_session:
            try:
                await RequestContext.from_request(request, db_session=db_session)
            except HTTPException as e:
                return JSONResponse(
                    status_code=e.status_code, content={"detail": e.detail}
                )
            return await call_next(request)

    async def get_request_context(self, request: Request) -> RequestContext:
        # If already cached by middleware, return it
        if hasattr(request.state, "ctx"):
            return request.state.ctx

        # Else, open a short-lived session just for user lookup
        async with self.db_session_factory.new_session() as session:
            return await RequestContext.from_request(request, db_session=session)


timelens_app = TimelensApp()
app = timelens_app.app
