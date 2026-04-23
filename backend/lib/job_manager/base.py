# job_manager.py
from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dal import DALJobs, DAOJobsCreate, DAOJobsUpdate
from backend.db.dal.base import safe_commit
from backend.db.data_models import JobStatus
from backend.env_loader import EnvLoader
from backend.lib.redis.client import RedisClient
from backend.lib.utils.common import none_throws, utcnow
from backend.worker.job_processor.types import (
    JobInputPayload,
    JobOutputPayload,
    PhotobookGenerationInputPayload,
)

from .types import JobQueue, JobType

DEFAULT_DEQUEUE_POLL_TIMEOUT_SECS = 5


JOB_TYPE_PAYLOAD_TYPE_REGISTRY = {
    JobType.PHOTOBOOK_GENERATION: PhotobookGenerationInputPayload
}


class JobManager:
    @classmethod
    def __get_queue_name(cls, queue: JobQueue) -> str:
        prefix = "PROD_" if EnvLoader.get_optional("ENV") == "production" else "DEV_"
        return prefix + str(queue)

    def __init__(self, redis: RedisClient, queue: JobQueue) -> None:
        self.redis = redis
        self.queue_name = JobManager.__get_queue_name(queue)

    async def enqueue(
        self, db_session: AsyncSession, job_type: JobType, job_payload: JobInputPayload
    ) -> UUID:
        # Step 1: Persist job in Postgres
        async with safe_commit(db_session):
            job = await DALJobs.create(
                db_session,
                DAOJobsCreate(
                    job_type=job_type,
                    status=JobStatus.QUEUED,
                    user_id=job_payload.user_id,
                    photobook_id=job_payload.originating_photobook_id,
                    input_payload=job_payload.model_dump(mode="json"),
                    result_payload=None,
                    error_message=None,
                    started_at=None,
                    completed_at=None,
                ),
            )

        # Step 2: Enqueue job ID in Redis
        await self.redis.safe_rpush(self.queue_name, str(job.id))
        return job.id

    async def poll(
        self,
        timeout: Optional[int] = DEFAULT_DEQUEUE_POLL_TIMEOUT_SECS,
    ) -> Optional[UUID]:
        result = await self.redis.safe_blpop(self.queue_name, timeout=timeout)
        if not result:
            return None  # timeout occurred

        _job_queue_name, job_id_str = result
        try:
            job_id = UUID(job_id_str)
            return job_id
        except ValueError:
            # Optionally log and skip
            return None

    async def claim(
        self, db_session: AsyncSession, job_id: UUID
    ) -> tuple[JobType, JobInputPayload]:
        job_obj = none_throws(
            await DALJobs.get_by_id(db_session, job_id), f"Job UUID: {job_id} not found"
        )
        job_type_str = none_throws(job_obj).job_type
        job_type_enum = JobType(job_type_str)
        if job_type_enum not in JOB_TYPE_PAYLOAD_TYPE_REGISTRY:
            raise Exception(f"{job_type_str} not in JOB_TYPE_PAYLOAD_TYPE_REGISTRY")
        job_payload_cls: type[JobInputPayload] = JOB_TYPE_PAYLOAD_TYPE_REGISTRY[
            job_type_enum
        ]
        payload = job_payload_cls.model_validate(job_obj.input_payload)
        async with safe_commit(db_session):
            # Update job status in Postgres
            await DALJobs.update_by_id(
                db_session,
                job_id,
                DAOJobsUpdate(
                    status=JobStatus.DEQUEUED,
                    started_at=utcnow(),
                ),
            )
        return (job_type_enum, payload)

    async def update_status(
        self,
        db_session: AsyncSession,
        job_id: UUID,
        status: JobStatus,
        error_message: Optional[str] = None,
        result_payload: Optional[JobOutputPayload] = None,
    ) -> None:
        async with safe_commit(db_session):
            update_data = DAOJobsUpdate(
                status=status,
                error_message=error_message,
                result_payload=None
                if result_payload is None
                else result_payload.model_dump(mode="json"),
                completed_at=utcnow() if status == JobStatus.DONE else None,
            )
            await DALJobs.update_by_id(db_session, job_id, update_data)
