from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from paper_agent.storage import make_blob_store, make_cache, make_checkpointer

from api.config import get_settings
from api.routes import chat

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Built here rather than at import time so a misconfigured backend fails
    # loudly at startup instead of on the first request.
    app.state.cache = make_cache(settings.cache_backend, settings.redis_url)
    app.state.blobs = make_blob_store(
        settings.blob_backend,
        settings.storage_dir,
        {
            "endpoint": settings.s3_endpoint,
            "access_key": settings.s3_access_key,
            "secret_key": settings.s3_secret_key,
            "bucket": settings.s3_bucket,
        },
    )
    # D3: pass this checkpointer into build_graph() and store the compiled
    # graph on app.state.graph. `memory` loses state on restart; switch
    # CHECKPOINT_BACKEND=sqlite on D7 to make restarts survivable.
    app.state.checkpointer = make_checkpointer(
        settings.checkpoint_backend, settings.sqlite_path, settings.database_url
    )
    yield


app = FastAPI(
    title="Paper Agent API",
    version="0.1.0",
    description="Academic Research Copilot — agent gateway",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "env": settings.app_env,
        "version": app.version,
        "backends": {
            "checkpoint": settings.checkpoint_backend,
            "cache": settings.cache_backend,
            "blob": settings.blob_backend,
        },
        # Model names and a key-presence flag only — never the key itself. D3
        # needs this to tell "credentials missing" from "upstream call failed"
        # in one glance, and the web badge already polls this endpoint.
        "llm": {
            "base_url": settings.llm_base_url,
            "chat_model": settings.llm_chat_model,
            "reasoning_model": settings.llm_reasoning_model,
            "key_present": bool(settings.llm_api_key),
        },
    }
