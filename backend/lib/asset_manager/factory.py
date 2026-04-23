import logging

from backend.env_loader import EnvLoader

from .base import AssetManager
from .local import LocalAssetManager
from .s3 import S3AssetManager


class AssetManagerFactory:
    def create(self) -> AssetManager:
        env = EnvLoader.get("ENV", "development").lower()
        if env == "production":
            logging.info(f"Using S3AssetManager under env {env}")
            return S3AssetManager()
        else:
            logging.info(f"Using LocalAssetManager under env {env}")
            return LocalAssetManager()
