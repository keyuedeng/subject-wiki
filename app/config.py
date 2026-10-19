"""Settings loaded from the environment / .env.

Secrets are SecretStr so they never appear in reprs, logs or tracebacks;
call .get_secret_value() only at the point of use.
"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    supabase_url: str
    supabase_service_key: SecretStr
    database_url: SecretStr

    storage_bucket: str = "subject-files"
    max_upload_bytes: int = 50 * 1024 * 1024

    # Phase 2: slide parsing. The key is only needed when a page takes the vision path.
    openai_api_key: SecretStr | None = None
    slides_model: str = "gpt-6.1-sol"
    slides_reasoning_effort: str = "medium"
    parse_concurrency: int = 4

    @field_validator("supabase_url")
    @classmethod
    def project_origin(cls, v: str) -> str:
        """Accept the dashboard's API URL too (https://x.supabase.co/rest/v1/)."""
        parts = urlsplit(v.strip())
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else v.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
