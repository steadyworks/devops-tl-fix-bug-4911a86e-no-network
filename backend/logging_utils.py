import logging

from .env_loader import EnvLoader


def configure_logging_env() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Remove all existing handlers (clean slate)
    while root_logger.hasHandlers():
        root_logger.removeHandler(root_logger.handlers[0])

    # Decide format based on env
    env = EnvLoader.get("ENV", "development").lower()
    if env == "production":
        log_format = "%(asctime)s [%(levelname)s] %(message)s"
        level = logging.INFO
    else:
        log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        level = logging.INFO

    formatter = logging.Formatter(log_format)
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)

    root_logger.addHandler(handler)
