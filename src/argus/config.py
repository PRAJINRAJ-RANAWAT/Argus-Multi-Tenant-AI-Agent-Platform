"""Runtime configuration, loaded from environment variables (prefix ARGUS_)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", env_file=".env", extra="ignore")

    env: str = "dev"
    service_name: str = "argus"
    database_url: str = "postgresql://argus:argus@localhost:5432/argus"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    git_sha: str = Field(default="unknown", description="Build revision, injected at image build")

    # GitHub App
    github_webhook_secret: SecretStr = SecretStr("")
    github_app_id: str = ""
    github_private_key: SecretStr = SecretStr("")  # PEM; literal "\\n" sequences are accepted
    github_private_key_path: str = ""
    github_api_url: str = "https://api.github.com"

    # LLM (OpenAI-compatible endpoint; the mock LLM in dev/tests)
    llm_base_url: str = "http://localhost:8090/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "mock-large"
    llm_timeout_s: float = 120.0

    # Workers
    worker_id: str = ""
    worker_concurrency: int = 20

    def github_private_key_pem(self) -> str:
        if self.github_private_key_path:
            with open(self.github_private_key_path) as f:
                return f.read()
        return self.github_private_key.get_secret_value().replace("\\n", "\n")


@lru_cache
def get_settings() -> Settings:
    return Settings()
