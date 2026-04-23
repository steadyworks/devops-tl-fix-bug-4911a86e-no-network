from uuid import UUID

from pydantic import BaseModel

from backend.db.dal import DALPhotobooks, FilterOp, OrderDirection
from backend.db.externals import PhotobooksOverviewResponse
from backend.route_handler.base import RouteHandler


class UserGetPhotobooksResponse(BaseModel):
    photobooks: list[PhotobooksOverviewResponse]


class UserAPIHandler(RouteHandler):
    def register_routes(self) -> None:
        self.router.add_api_route(
            "/api/user/{user_id}/photobooks",
            self.user_get_photobooks,
            methods=["GET"],
            response_model=UserGetPhotobooksResponse,
        )

    async def user_get_photobooks(
        self,
        user_id: UUID,
    ) -> UserGetPhotobooksResponse:
        async with self.app.new_db_session() as db_session:
            photobooks = await DALPhotobooks.list_all(
                db_session,
                {"user_id": (FilterOp.EQ, user_id)},
                order_by=[("updated_at", OrderDirection.DESC)],
            )
            resp = UserGetPhotobooksResponse(
                photobooks=PhotobooksOverviewResponse.from_daos(photobooks)
            )
            return resp
