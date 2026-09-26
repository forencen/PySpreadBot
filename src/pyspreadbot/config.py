"""Read configuration from environment variables and a local .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    app_name: str
    log_level: str


def load_settings(env_file: str | Path = ".env") -> Settings:
    """Load the specified .env file; existing environment variables take priority."""
    load_dotenv(dotenv_path=env_file, override=False)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    return Settings(
        app_name=os.getenv("APP_NAME", "PySpreadBot"),
        log_level=log_level,
    )
