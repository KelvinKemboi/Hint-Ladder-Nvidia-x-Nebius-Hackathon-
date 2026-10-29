from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env, so backend and docs share one config file.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Runtime configuration, read from the environment or the repo-root .env file."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_project_id: str = ""  # needed by the Token Factory sandbox only

    # Model IDs per role
    model_fast: str = ""
    model_deep: str = ""
    model_gate: str = ""

    llm_timeout_s: float = 120.0
    llm_max_retries: int = 2

    # "contree" = Token Factory sandbox; "local" = subprocess on this machine, dev only.
    sandbox_backend: Literal["contree", "local"] = "contree"
    sandbox_image: str = "sparring-partner-python:v1"  # built by scripts/build_sandbox_image.py
    sandbox_timeout_s: float = 60.0
    sandbox_memory_mb: int = 512

    # Bug hunt: Hypothesis inputs per property, and the time limit for one call of the user's code.
    explore_max_examples: int = 200
    explore_call_timeout_s: float = 1.0

    # Demo only: GET /api/sessions/{id}/gate-log shows blocked hints (and so the answers).
    expose_gate_log: bool = False

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
