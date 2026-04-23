from uuid import UUID

from pydantic import BaseModel


class JobInputPayload(BaseModel):
    user_id: UUID
    originating_photobook_id: UUID


class JobOutputPayload(BaseModel):
    job_id: UUID


class PhotobookGenerationInputPayload(JobInputPayload):
    asset_ids: list[UUID]


class PhotobookGenerationOutputPayload(JobOutputPayload):
    pass
