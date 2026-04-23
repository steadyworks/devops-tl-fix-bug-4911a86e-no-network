from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dal import (
    DALAssets,
    DALPages,
    DALPagesAssetsRel,
    DAOPagesUpdate,
    FilterOp,
    OrderDirection,
    safe_commit,
)
from backend.db.data_models import DAOPages
from backend.db.externals import AssetsOverviewResponse, PagesOverviewResponse
from backend.lib.asset_manager.base import AssetManager
from backend.route_handler.base import RouteHandler


class PageTextEditRequest(BaseModel):
    new_text: str


class PagesFullResponse(PagesOverviewResponse):
    assets: list[AssetsOverviewResponse]

    @classmethod
    async def rendered_from_daos(
        cls,
        pages: list[DAOPages],
        db_session: AsyncSession,
        asset_manager: AssetManager,
    ) -> list["PagesFullResponse"]:
        page_ids = [page.id for page in pages]
        page_asset_rels = await DALPagesAssetsRel.list_all(
            db_session,
            filters={"page_id": (FilterOp.IN, page_ids)},
            order_by=[("order_index", OrderDirection.ASC)],
        )

        # Step 4: Collect all asset_ids used
        asset_ids = [rel.asset_id for rel in page_asset_rels if rel.asset_id]
        asset_list = await DALAssets.get_by_ids(db_session, asset_ids)
        assets_by_id = {asset.id: asset for asset in asset_list}

        # Step 5: Generate signed URLs for original asset keys
        asset_keys = [
            asset.asset_key_original for asset in asset_list if asset.asset_key_original
        ]
        signed_urls = await asset_manager.generate_signed_urls_batched(asset_keys)

        # Step 6: Assemble response
        page_id_to_assets: dict[UUID, list[AssetsOverviewResponse]] = {}
        for rel in page_asset_rels:
            if rel.page_id and rel.asset_id:
                asset = assets_by_id[rel.asset_id]
                signed_url = signed_urls.get(asset.asset_key_original)
                # Inject signed URL into the model
                asset_with_url = AssetsOverviewResponse(
                    **asset.model_dump(),
                    signed_asset_url=(
                        signed_url if isinstance(signed_url, str) else ""
                    ),
                )

                page_id_to_assets.setdefault(rel.page_id, []).append(asset_with_url)
        return [
            cls(
                **PagesOverviewResponse.from_dao(page).model_dump(),
                assets=page_id_to_assets.get(page.id, []),
            )
            for page in pages
        ]


class PageAPIHandler(RouteHandler):
    def register_routes(self) -> None:
        self.router.add_api_route(
            "/api/page/{page_id}/edit_text",
            self.page_edit_text,
            methods=["POST"],
            response_model=PagesOverviewResponse,
        )

    async def page_edit_text(
        self,
        page_id: UUID,
        payload: PageTextEditRequest,
    ) -> PagesOverviewResponse:
        async with self.app.new_db_session() as db_session:
            async with safe_commit(db_session):
                updated_page = await DALPages.update_by_id(
                    db_session, page_id, DAOPagesUpdate(user_message=payload.new_text)
                )
            return PagesOverviewResponse.from_dao(updated_page)
