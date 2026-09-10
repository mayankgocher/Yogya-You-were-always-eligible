"""Central configuration for Yogya.

Everything the app needs to boot is resolved here so that no module reaches
into os.environ directly. Tests override settings by constructing Settings()
explicitly rather than by mutating global state.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
CORPUS_DIR = PACKAGE_ROOT / "data" / "corpus"


class Settings(BaseSettings):
    """Runtime settings, populated from environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="YOGYA_", extra="ignore"
    )

    # --- LLM ---------------------------------------------------------------
    # Any LiteLLM-supported model string works, e.g.
    #   groq/llama-3.3-70b-versatile
    #   gemini/gemini-2.0-flash
    #   ollama/llama3.2
    llm_model: str = "groq/llama-3.3-70b-versatile"
    llm_fallback_models: list[str] = Field(default_factory=list)
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1600
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # When true, no network call is ever made; the deterministic FakeLLM is used.
    # Tests set this. It is also the default when no provider key is present.
    llm_offline: bool = False

    # --- Data --------------------------------------------------------------
    corpus_dir: Path = CORPUS_DIR
    corpus_version: str = "latest"

    # --- Retrieval ---------------------------------------------------------
    retrieval_top_k: int = 40

    # --- Graph -------------------------------------------------------------
    max_appeal_attempts: int = 2
    # Every state-changing submission pauses for a human by default.
    require_human_approval: bool = True

    # --- API ---------------------------------------------------------------
    api_title: str = "Yogya — Entitlement Finder"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @property
    def web_dir(self) -> Path:
        return PROJECT_ROOT / "web"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests after mutating the environment."""
    get_settings.cache_clear()
