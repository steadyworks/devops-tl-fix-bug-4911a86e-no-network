import asyncio
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from mypy_boto3_s3 import S3Client

from backend.env_loader import EnvLoader
from backend.lib.types.asset import Asset

from .base import AssetManager, AssetStorageKey

MAX_CONCURRENT_UPLOADS = 25
MAX_CONCURRENT_DOWNLOADS = 50


class S3AssetManager(AssetManager):
    def __init__(
        self,
        bucket_name: Optional[str] = None,
        region_name: Optional[str] = None,
    ):
        """Initializes the S3 client and target bucket.

        Args:
            bucket_name: Target S3 bucket.
            region_name: Optional AWS region (default uses env config).

        """
        self.bucket_name = bucket_name or EnvLoader.get("AWS_S3_DEFAULT_BUCKET_NAME")
        self.region_name = region_name or EnvLoader.get("AWS_S3_DEFAULT_BUCKET_REGION")
        self.__upload_semaphore = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        self.__download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self.s3: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            region_name=self.region_name,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def upload_file(
        self,
        src_file_path: Path,
        dest_key: AssetStorageKey,
    ) -> Asset:
        """Uploads a file from local disk to the configured S3 bucket asynchronously.

        Args:
            file_path: Local path to the file.
            key: The S3 object key.
            public: Whether to make the file publicly accessible.
            content_type: Optional MIME type.
        """
        async with self.__upload_semaphore:
            asset = Asset(
                cached_local_path=src_file_path,
                asset_storage_key=dest_key,
            )
            await asyncio.to_thread(
                self.s3.upload_file,
                Filename=str(src_file_path),
                Bucket=self.bucket_name,
                Key=dest_key,
                ExtraArgs={"ContentType": await asset.mime_type()},
            )
            return asset

    async def download_file(
        self, src_key: AssetStorageKey, dest_file_path: Path
    ) -> Asset:
        """Downloads an object from S3 and saves it to the local file path.

        Args:
            src_key: S3 object key.
            dest_file_path: Destination path on local disk.
        """
        async with self.__download_semaphore:
            await asyncio.to_thread(
                self.s3.download_file,
                Bucket=self.bucket_name,
                Key=src_key,
                Filename=str(dest_file_path),
            )
            return Asset(
                cached_local_path=dest_file_path,
                asset_storage_key=src_key,
            )

    async def generate_signed_url(
        self, src_key: AssetStorageKey, expires_in: int = 3600
    ) -> str:
        """Asynchronously generate a signed URL for an S3 object.

        Args:
            src_key: S3 object key (e.g., "uploads/uuid.png").
            expires_in: Time in seconds before the URL expires.

        Returns:
            A signed URL as a string.

        """
        return await asyncio.to_thread(
            self.s3.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self.bucket_name, "Key": src_key},
            ExpiresIn=expires_in,
        )
