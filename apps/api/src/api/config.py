from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables and .env.

    Both `<repo>/.env` and `apps/api/.env` are read so the same file works
    whether uvicorn is launched from the repo root or from apps/api.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- Storage backends -------------------------------------------------
    # Defaults are local-first: the app boots with zero external services.
    # Flip to postgres/redis/s3 when Docker (or a native Postgres) is available.
    checkpoint_backend: str = "memory"  # memory | sqlite | postgres
    cache_backend: str = "memory"  # memory | redis
    blob_backend: str = "local"  # local | s3
    sqlite_path: str = "data/checkpoints.sqlite"
    storage_dir: str = "data/blobs"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/agent"
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "papers"

    # --- LLM provider ----------------------------------------------------
    # DeepSeek is OpenAI-compatible, so one client + base_url covers it.
    # Repoint llm_base_url at OpenAI / OpenRouter / a local Ollama to switch
    # providers without touching any calling code.
    #
    # SecretStr, not str: these settings get handed to Langfuse and can end up
    # in repr()s, tracebacks and structured logs. SecretStr renders as
    # "**********" unless explicitly unwrapped with .get_secret_value().
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: SecretStr | None = None
    # Fast Path (ReAct + tool calling) and the cheap graph nodes
    llm_chat_model: str = "deepseek-flash"
    # Deep Path nodes that need multi-step reasoning (Verifier, outline planning)
    llm_reasoning_model: str = "deepseek-v4-pro"
    # Both models emit reasoning_content and those tokens count against this
    # budget. Set it too low and the model spends everything reasoning and never
    # emits the tool call -- reads like broken tool calling, is actually this.
    llm_max_tokens: int = 2048
    # Low, not zero. Temperature 0 does not make output reproducible anyway --
    # MoE routing, batch composition and float accumulation order all vary -- so
    # the honest goal is a stable distribution, not a fixed string. What 0 would
    # buy is degenerate repetition on long generations, which is worse than the
    # variance it removes.
    llm_temperature: float = 0.2

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    typesafe_api_key: SecretStr | None = None
    decision_model_backend: str = "jev"
    jev_timeout_ms: int = 800

    openalex_mailto: str | None = None
    semantic_scholar_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
