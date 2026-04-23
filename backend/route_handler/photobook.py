import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Self
from uuid import UUID

from fastapi import File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dal import (
    DALAssets,
    DALPages,
    DALPhotobooks,
    DAOAssetsCreate,
    DAOPagesUpdate,
    DAOPhotobooksCreate,
    DAOPhotobooksUpdate,
    FilterOp,
    OrderDirection,
    safe_commit,
)
from backend.db.data_models import DAOPhotobooks, PhotobookStatus, UserProvidedOccasion
from backend.db.externals import PhotobooksOverviewResponse
from backend.lib.asset_manager.base import AssetManager
from backend.lib.job_manager.types import JobType
from backend.lib.types.asset import Asset
from backend.lib.utils.common import none_throws
from backend.lib.utils.web_requests import UploadFileTempDirManager
from backend.route_handler.base import RouteHandler
from backend.worker.job_processor.types import PhotobookGenerationInputPayload

from .page import PagesFullResponse

if TYPE_CHECKING:
    from pathlib import Path

    from backend.lib.asset_manager.base import AssetStorageKey


class UploadedFileInfo(BaseModel):
    filename: str
    storage_key: str


class FailedUploadInfo(BaseModel):
    filename: str
    error: str


class NewPhotobookResponse(BaseModel):
    job_id: UUID
    photobook_id: UUID
    uploaded_files: list[UploadedFileInfo]
    failed_uploads: list[FailedUploadInfo]
    skipped_non_media: list[str]


class PhotobookEditTitleRequest(BaseModel):
    new_title: str


class PhotobooksFullResponse(PhotobooksOverviewResponse):
    pages: list[PagesFullResponse]

    @classmethod
    async def rendered_from_dao(
        cls,
        dao: DAOPhotobooks,
        db_session: AsyncSession,
        asset_manager: AssetManager,
    ) -> Self:
        resp = await super().rendered_from_dao(
            dao,
            db_session,
            asset_manager,
        )
        pages = await DALPages.list_all(
            db_session,
            {"photobook_id": (FilterOp.EQ, dao.id)},
            order_by=[("page_number", OrderDirection.ASC)],
        )
        pages_response_full = await PagesFullResponse.rendered_from_daos(
            pages, db_session, asset_manager
        )
        return resp.model_copy(update={"pages": pages_response_full})


class EditPageRequest(BaseModel):
    page_id: UUID
    new_user_message: str


class PhotobookEditPagesRequest(BaseModel):
    edits: list[EditPageRequest]


class PhotobookAPIHandler(RouteHandler):
    def register_routes(self) -> None:
        self.router.add_api_route(
            "/api/photobook/new",
            self.photobook_new,
            methods=["POST"],
            response_model=NewPhotobookResponse,
        )
        self.router.add_api_route(
            "/api/photobook/{photobook_id}",
            self.get_photobook_by_id,
            methods=["GET"],
            response_model=PhotobooksFullResponse,
        )
        self.router.add_api_route(
            "/api/photobook/{photobook_id}/edit_title",
            self.photobook_edit_title,
            methods=["POST"],
            response_model=PhotobooksFullResponse,
        )
        self.router.add_api_route(
            "/api/photobook/{photobook_id}/edit_pages",
            self.photobook_edit_pages,
            methods=["POST"],
            response_model=PhotobooksFullResponse,
        )

    @staticmethod
    def is_accepted_mime(mime: Optional[str]) -> bool:
        return mime is not None and (
            mime.startswith("image/")
            # or mime.startswith("video/") # FIXME / TODO: only images allowed for now
        )

    async def photobook_new(
        self,
        request: Request,
        files: list[UploadFile] = File(...),
        user_provided_occasion: UserProvidedOccasion = Form(...),
        user_provided_custom_details: Optional[str] = Form(None),
        user_provided_context: Optional[str] = Form(None),
    ) -> NewPhotobookResponse:
        request_context = await self.get_request_context(request)

        # Filter valid files according to FastAPI reported mime type
        valid_files = [
            file
            for file in files
            if PhotobookAPIHandler.is_accepted_mime(file.content_type)
        ]
        file_names = [file.filename for file in valid_files]
        skipped = [
            file.filename
            for file in files
            if file not in valid_files and file.filename is not None
        ]
        logging.info({"accepted_files": file_names, "skipped_non_media": skipped})

        succeeded_uploads: list[UploadedFileInfo] = []
        failed_uploads: list[FailedUploadInfo] = []

        async with UploadFileTempDirManager(
            str(uuid.uuid4()),
            valid_files,  # FIXME: Use libmagic to check against actual MIME
        ) as user_requested_uploads:
            # 1. Create photobook in DB
            async with self.app.new_db_session() as db_session:
                async with safe_commit(db_session):
                    photobook = await DALPhotobooks.create(
                        db_session,
                        DAOPhotobooksCreate(
                            user_id=request_context.user_id,
                            title=f"New Photobook {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                            caption=None,
                            theme=None,
                            status=PhotobookStatus.PENDING,
                            user_provided_occasion=user_provided_occasion,
                            user_provided_occasion_custom_details=user_provided_custom_details,
                            user_provided_context=user_provided_context,
                            thumbnail_asset_id=None,
                        ),
                    )

            # 2. Upload to AssetManager
            upload_inputs: list[tuple[Path, AssetStorageKey]] = [
                (
                    none_throws(asset.cached_local_path),
                    self.app.asset_manager.mint_asset_key(
                        photobook.id, none_throws(asset.cached_local_path).name
                    ),
                )
                for (_original_fname, asset) in user_requested_uploads
            ]
            upload_results = await self.app.asset_manager.upload_files_batched(
                upload_inputs
            )
            asset_objs_to_create: list[DAOAssetsCreate] = []

            # 3. Transform upload results into endpoint response
            for _original_fname, asset in user_requested_uploads:
                upload_res = upload_results.get(
                    none_throws(asset.cached_local_path), None
                )
                if upload_res is None or isinstance(upload_res, Exception):
                    failed_uploads.append(
                        FailedUploadInfo(
                            filename=_original_fname, error=str(upload_res)
                        )
                    )
                else:
                    assert isinstance(upload_res, Asset)
                    succeeded_uploads.append(
                        UploadedFileInfo(
                            filename=_original_fname,
                            storage_key=none_throws(upload_res.asset_storage_key),
                        )
                    )
                    asset_objs_to_create.append(
                        DAOAssetsCreate(
                            user_id=request_context.user_id,
                            asset_key_original=none_throws(
                                upload_res.asset_storage_key
                            ),
                            asset_key_display=None,
                            asset_key_llm=None,
                            metadata_json={},
                            original_photobook_id=photobook.id,
                        )
                    )

            async with self.app.new_db_session() as db_session:
                # 3. Batch-insert assets
                async with safe_commit(db_session):
                    created_assets = await DALAssets.create_many(
                        db_session, asset_objs_to_create
                    )

                # 4. Enqueue photobook generation job
                job_id = await self.app.job_manager.enqueue(
                    db_session,
                    JobType.PHOTOBOOK_GENERATION,
                    PhotobookGenerationInputPayload(
                        user_id=request_context.user_id,
                        originating_photobook_id=photobook.id,
                        asset_ids=[asset.id for asset in created_assets],
                    ),
                )

        return NewPhotobookResponse(
            job_id=job_id,
            photobook_id=photobook.id,
            uploaded_files=succeeded_uploads,
            failed_uploads=failed_uploads,
            skipped_non_media=skipped,
        )

    async def get_photobook_by_id(
        self,
        photobook_id: UUID,
    ) -> PhotobooksFullResponse:
        async with self.app.new_db_session() as db_session:
            # Step 1: Fetch photobook
            photobook = await DALPhotobooks.get_by_id(db_session, photobook_id)
            if photobook is None:
                raise HTTPException(status_code=404, detail="Photobook not found")
            return await PhotobooksFullResponse.rendered_from_dao(
                photobook, db_session, self.app.asset_manager
            )

    async def photobook_edit_title(
        self, photobook_id: UUID, payload: PhotobookEditTitleRequest
    ) -> PhotobooksOverviewResponse:
        async with self.app.new_db_session() as db_session:
            async with safe_commit(db_session):
                photobook = await DALPhotobooks.update_by_id(
                    db_session,
                    photobook_id,
                    DAOPhotobooksUpdate(
                        title=payload.new_title,
                    ),
                )
            return PhotobooksOverviewResponse.from_dao(photobook)

    async def photobook_edit_pages(
        self, photobook_id: UUID, payload: PhotobookEditPagesRequest
    ) -> PhotobooksFullResponse:
        async with self.app.new_db_session() as db_session:
            # 1. Validate photobook exists
            photobook = await DALPhotobooks.get_by_id(db_session, photobook_id)
            if photobook is None:
                raise HTTPException(status_code=404, detail="Photobook not found")

            # 2. Batch apply page updates
            async with safe_commit(db_session):
                update_map = {
                    edit.page_id: DAOPagesUpdate(user_message=edit.new_user_message)
                    for edit in payload.edits
                }
                await DALPages.update_many_by_id(db_session, update_map)

            # 3. Return updated photobook and its pages
            return await PhotobooksFullResponse.rendered_from_dao(
                photobook, db_session, self.app.asset_manager
            )
