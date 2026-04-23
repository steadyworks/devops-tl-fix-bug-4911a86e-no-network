import os
import threading
from typing import Final, Optional

from dotenv import load_dotenv

from backend.path_manager import PathManager


class EnvLoader:
    _lock: Final[threading.Lock] = threading.Lock()
    _loaded: bool = False

    @classmethod
    def _load_env_once(cls) -> None:
        if not cls._loaded:
            with cls._lock:
                if not cls._loaded:
                    env = os.getenv("ENV")
                    env_file = ".env.prod" if env == "production" else ".env.dev"
                    env_path = PathManager().get_repo_root() / env_file

                    loaded = load_dotenv(dotenv_path=env_path)
                    if not loaded:
                        raise RuntimeError(
                            f"Failed to load environment from {env_path}"
                        )

                    cls._loaded = True

    @classmethod
    def get(cls, key: str, default_value: Optional[str] = None) -> str:
        cls._load_env_once()
        val = os.getenv(key)
        if val is None:
            if default_value is not None:
                return default_value
            raise KeyError(f"Missing environment variable: {key}")
        return val

    @classmethod
    def get_optional(cls, key: str) -> Optional[str]:
        cls._load_env_once()
        val = os.getenv(key)
        return val
