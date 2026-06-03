from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/project_hub.db")
    encryption_key: str | None = os.getenv("ENCRYPTION_KEY")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    gamma_api_key: str | None = os.getenv("GAMMA_API_KEY")
    gamma_base_url: str = os.getenv("GAMMA_BASE_URL", "https://public-api.gamma.app/v1.0")
    gamma_language: str = os.getenv("GAMMA_LANGUAGE", "ru")
    gamma_template_id: str | None = os.getenv(
        "GAMMA_TEMPLATE_ID", "Bankiros-m15hcuhf4vcyszl"
    ) or None
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "600"))
    llm_max_context_chars: int = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "80000"))
    llm_max_field_chars: int = int(os.getenv("LLM_MAX_FIELD_CHARS", "4000"))
    llm_retry_count: int = int(os.getenv("LLM_RETRY_COUNT", "3"))
    gemini_fallback_to_flash: bool = os.getenv(
        "GEMINI_FALLBACK_TO_FLASH", "true"
    ).lower() in ("1", "true", "yes")
    gemini_base_url: str | None = os.getenv("GOOGLE_GEMINI_BASE_URL") or os.getenv(
        "GEMINI_BASE_URL"
    )
    default_llm_model: str | None = os.getenv("DEFAULT_LLM_MODEL")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    jobs_queue_name: str = os.getenv("JOBS_QUEUE_NAME", "arq:queue")
    jobs_worker_timeout: int = int(os.getenv("JOBS_WORKER_TIMEOUT", "1800"))
    jobs_worker_concurrency: int = int(os.getenv("JOBS_WORKER_CONCURRENCY", "4"))

    @property
    def available_models(self) -> dict[str, bool]:
        deepseek = bool(self.deepseek_api_key)
        return {
            "gemini-2.5-pro": bool(self.gemini_api_key),
            "gemini-2.5-flash": bool(self.gemini_api_key),
            "claude-3-5-sonnet": bool(self.anthropic_api_key),
            "deepseek-v3": deepseek,
            "deepseek-r1": deepseek,
        }

    def resolve_default_model(self, fallback: str = "gemini-2.5-pro") -> str:
        """Модель по умолчанию в UI: DEFAULT_LLM_MODEL или fallback, если ключ есть."""
        if self.default_llm_model and self.available_models.get(self.default_llm_model):
            return self.default_llm_model
        if self.available_models.get(fallback):
            return fallback
        for key in ("deepseek-v3", "claude-3-5-sonnet", "gemini-2.5-flash", "gemini-2.5-pro"):
            if self.available_models.get(key):
                return key
        return fallback


@lru_cache
def get_settings() -> Settings:
    return Settings()
