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

    # GitHub OAuth (user login). The GitHub App's client id/secret.
    github_web_url: str = "https://github.com"
    github_oauth_client_id: str = ""
    github_oauth_client_secret: SecretStr = SecretStr("")
    public_url: str = "http://localhost:8000"  # where users reach the API (OAuth callback)
    session_ttl_s: int = 7 * 24 * 3600
    cookie_secure: bool = True
    # Public read-only demo: /demo signs visitors into this tenant (GitHub account login of
    # the demo installation) as viewers. Empty = no public demo.
    demo_tenant: str = ""
    demo_ip_questions_per_10min: int = 5
    demo_ip_questions_per_day: int = 20
    demo_questions_per_day: int = 300  # across all visitors
    demo_sessions_per_ip_per_hour: int = 30
    # Behind a reverse proxy (Caddy) the client IP comes from X-Forwarded-For.
    trust_proxy_headers: bool = False
    # Local demo only: /auth/dev-login signs in without GitHub. Requires env == "dev".
    dev_login: bool = False
    repo_permission_cache_s: int = 300

    # LLM (OpenAI-compatible endpoint; the mock LLM in dev/tests)
    llm_base_url: str = "http://localhost:8090/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "mock-large"
    llm_timeout_s: float = 120.0
    # LLM gateway. Routes: tier -> ordered model chain "provider:model" (first = primary).
    llm_routes: str = (
        '{"large": ["openai:mock-large", "openai:mock-small"],'
        ' "small": ["openai:mock-small", "openai:mock-large"]}'
    )
    anthropic_base_url: str = ""  # e.g. https://api.anthropic.com/v1 (empty = disabled)
    anthropic_api_key: SecretStr = SecretStr("")
    tenant_tokens_per_min: int = 400_000
    provider_requests_per_min: int = 600
    qa_cache_threshold: float = 0.95
    online_eval_rate: float = 0.05  # share of successful reviews scored by the LLM judge
    # Embeddings (OpenAI-compatible /embeddings; defaults to the mock LLM server).
    embed_base_url: str = ""  # empty = llm_base_url
    embed_api_key: SecretStr = SecretStr("")
    embed_model: str = "mock-embed"

    # Guardrails: extra hosts agents' HTTP clients may reach (comma-separated; the
    # GitHub, LLM and embedding hosts above are always allowed).
    egress_extra_hosts: str = ""

    def egress_hosts(self) -> set[str]:
        from argus.guardrails.egress import hosts_from_urls

        extra = {h.strip() for h in self.egress_extra_hosts.split(",") if h.strip()}
        return extra | hosts_from_urls(
            self.github_api_url,
            self.github_web_url,
            self.llm_base_url,
            self.embed_base_url,
            self.anthropic_base_url,
        )

    # Observability: OTLP/HTTP endpoint for traces (Jaeger, Tempo, a collector, or
    # Langfuse's /api/public/otel) and optional headers ("k=v,k2=v2", e.g. auth).
    otel_endpoint: str = ""
    otel_headers: SecretStr = SecretStr("")
    metrics_port: int = 9100  # worker and relay serve /metrics here

    def otel_header_map(self) -> dict[str, str]:
        raw = self.otel_headers.get_secret_value()
        pairs = (p.split("=", 1) for p in raw.split(",") if "=" in p)
        return {k.strip(): v.strip() for k, v in pairs}

    # Workers
    worker_id: str = ""
    # Bearer token for operator endpoints (/admin: DLQ). Empty = admin API off.
    admin_token: SecretStr = SecretStr("")
    worker_concurrency: int = 20
    index_concurrency: int = 4
    # Fair scheduling: these topics get per-tenant lanes, each tenant capped at
    # tenant_concurrency in-flight jobs across all workers.
    fair_topics: str = "jobs:review,jobs:qa"
    tenant_concurrency: int = 4
    # Load shedding: indexing and evals pause while interactive work lags this long.
    shed_low_priority_after_s: float = 60.0

    def fair_topic_set(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.fair_topics.split(",") if t.strip())

    def github_private_key_pem(self) -> str:
        if self.github_private_key_path:
            with open(self.github_private_key_path) as f:
                return f.read()
        return self.github_private_key.get_secret_value().replace("\\n", "\n")


@lru_cache
def get_settings() -> Settings:
    return Settings()
