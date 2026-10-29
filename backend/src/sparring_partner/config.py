from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, so backend and docs share one config file.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Runtime configuration, read from the environment or the repo-root .env file."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"

    # Model IDs per role (see docs/PROJECT_OUTLINE.md §5.2).
    model_fast: str = ""
    model_deep: str = ""
    model_gate: str = ""

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
