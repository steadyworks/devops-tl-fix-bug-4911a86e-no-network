from fastapi.responses import JSONResponse

from backend.route_handler.base import RouteHandler


class DebugHandler(RouteHandler):
    def register_routes(self) -> None:
        self.router.add_api_route("/api/debug", self.debug, methods=["GET"])
        self.router.add_api_route(
            "/api/debug/sentry-debug",
            self.sentry_debug,
            methods=["GET"],
        )

    async def debug(self) -> JSONResponse:
        return JSONResponse({"hello": "world"})

    async def sentry_debug(self) -> JSONResponse:
        _division_by_zero = 1 / 0
        return JSONResponse("")
