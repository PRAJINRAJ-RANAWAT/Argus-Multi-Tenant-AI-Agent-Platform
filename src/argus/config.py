"""Runtime configuration, loaded from environment variables (prefix ARGUS_)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "argus"
    database_url: str = "postgresql://argus:argus@localhost:5432/argus"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    git_sha: str = Field(default="unknown", description="Build revision, injected at image build")


@lru_cache
def get_settings() -> Settings:
    return Settings()
