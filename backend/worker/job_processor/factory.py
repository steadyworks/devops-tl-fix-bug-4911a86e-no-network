from typing import Any
from uuid import UUID

from backend.db.session.factory import AsyncSessionFactory
from backend.lib.asset_manager.base import AssetManager
from backend.lib.job_manager.types import JobType

from .base import JobProcessor
from .photobook_generation import PhotobookGenerationJobProcessor

# Registry with erased generics
JOB_TYPE_JOB_PROCESSOR_REGISTRY: dict[JobType, type[JobProcessor[Any, Any]]] = {
    JobType.PHOTOBOOK_GENERATION: PhotobookGenerationJobProcessor,
}


class JobProcessorFactory:
    @classmethod
    def new_processor(
        cls,
        job_uuid: UUID,
        job_type: JobType,
        asset_manager: AssetManager,
        db_session_factory: AsyncSessionFactory,
    ) -> JobProcessor[Any, Any]:
        processor_cls = JOB_TYPE_JOB_PROCESSOR_REGISTRY.get(job_type)
        if processor_cls is None:
            raise Exception(f"{job_type} not found")
        return processor_cls(
            job_uuid=job_uuid,
            job_type=job_type,
            asset_manager=asset_manager,
            db_session_factory=db_session_factory,
        )
