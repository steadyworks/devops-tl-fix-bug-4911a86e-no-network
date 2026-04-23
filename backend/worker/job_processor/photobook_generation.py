import logging
import tempfile
from pathlib import Path
from typing import Optional
from uuid import UUID

from backend.db.dal import (
    DALAssets,
    DALPages,
    DALPagesAssetsRel,
    DALPhotobooks,
    DAOPagesAssetsRelCreate,
    DAOPagesCreate,
    DAOPhotobooksUpdate,
)
from backend.db.dal.base import safe_commit
from backend.db.data_models import PhotobookStatus
from backend.db.session.factory import AsyncSessionFactory
from backend.lib.asset_manager.base import AssetManager
from backend.lib.job_manager.types import JobType
from backend.lib.types.asset import Asset
from backend.lib.utils.common import none_throws
from backend.lib.vertex_ai.gemini import Gemini

from .base import JobProcessor
from .types import PhotobookGenerationInputPayload, PhotobookGenerationOutputPayload


class PhotobookGenerationJobProcessor(
    JobProcessor[PhotobookGenerationInputPayload, PhotobookGenerationOutputPayload]
):
    def __init__(
        self,
        job_uuid: UUID,
        job_type: JobType,
        asset_manager: AssetManager,
        db_session_factory: AsyncSessionFactory,
    ) -> None:
        super().__init__(job_uuid, job_type, asset_manager, db_session_factory)
        self.gemini = Gemini()

    async def process(
        self, input_payload: PhotobookGenerationInputPayload
    ) -> PhotobookGenerationOutputPayload:
        asset_ids = input_payload.asset_ids
        async with self.db_session_factory.new_session() as db_session:
            asset_objs = await DALAssets.get_by_ids(db_session, asset_ids)
            orig_image_keys = [obj.asset_key_original for obj in asset_objs]
            originating_photobook = none_throws(
                await DALPhotobooks.get_by_id(
                    db_session, input_payload.originating_photobook_id
                ),
                f"originating_photobook: {input_payload.originating_photobook_id} not found",
            )

        logging.info(
            f"[job-processor] Processing job {self.job_uuid} created from photobook: "
            f"{input_payload.originating_photobook_id}"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            download_results = await self.asset_manager.download_files_batched(
                [(key, tmp_path / Path(key).name) for key in orig_image_keys]
            )
            downloaded_paths = [
                none_throws(asset.cached_local_path)
                for asset in download_results.values()
                if isinstance(asset, Asset)
            ]
            if not downloaded_paths:
                raise RuntimeError("All image downloads failed")

            img_filename_assets_map = {
                Path(asset.asset_key_original).name: asset for asset in asset_objs
            }

            gemini_output = None
            try:
                gemini_output = await self.gemini.run_image_understanding_job(
                    downloaded_paths,
                    originating_photobook.user_provided_occasion,
                    originating_photobook.user_provided_occasion_custom_details,
                    originating_photobook.user_provided_context,
                )
            except Exception as e:
                raise Exception(
                    f"Gemini call fail. gemini_output={gemini_output}. Exception: {e}"
                )

        async with self.db_session_factory.new_session() as db_session:
            async with safe_commit(db_session):
                page_create_objs = [
                    DAOPagesCreate(
                        photobook_id=originating_photobook.id,
                        page_number=idx,
                        layout=None,
                        user_message=page_schema.page_message,
                        user_message_alternative_options=page_schema.page_message_alternatives_serialized(),
                    )
                    for idx, page_schema in enumerate(gemini_output.photobook_pages)
                ]
                pages = await DALPages.create_many(db_session, page_create_objs)

                first_asset_id: Optional[UUID] = None
                pages_assets_rel_creates: list[DAOPagesAssetsRelCreate] = []
                for page_schema, page in zip(gemini_output.photobook_pages, pages):
                    for idx, page_photo in enumerate(page_schema.page_photos):
                        asset_nullable = img_filename_assets_map.get(page_photo, None)
                        if asset_nullable is not None:
                            pages_assets_rel_creates.append(
                                DAOPagesAssetsRelCreate(
                                    page_id=page.id,
                                    asset_id=asset_nullable.id,
                                    order_index=idx,
                                    caption=None,
                                )
                            )
                            if first_asset_id is None:
                                first_asset_id = asset_nullable.id

                await DALPagesAssetsRel.create_many(
                    db_session, pages_assets_rel_creates
                )
                await DALPhotobooks.update_by_id(
                    db_session,
                    originating_photobook.id,
                    DAOPhotobooksUpdate(
                        status=PhotobookStatus.DRAFT,
                        title=gemini_output.photobook_title,
                        thumbnail_asset_id=None
                        if first_asset_id is None
                        else first_asset_id,
                    ),
                )

        return PhotobookGenerationOutputPayload(job_id=self.job_uuid)
