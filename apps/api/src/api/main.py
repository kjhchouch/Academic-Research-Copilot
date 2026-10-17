import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from paper_agent.graph import build_graph
from paper_agent.llm import make_chat_model
from paper_agent.observability import make_tracer, shutdown_tracer, verify_tracer
from paper_agent.storage import make_blob_store, make_cache, make_checkpointer
from paper_agent.tools import TOOLS

from api.config import get_settings
from api.routes import chat

settings = get_settings()
logger = logging.getLogger(__name__)


def _build_graph(checkpointer: object) -> object | None:
    """Compile the Fast Path graph, or return None when there is no credential.

    None is a deliberate outcome, not a swallowed error. Building ChatOpenAI
    without a key produces an object that fails on first use, which turns a
    configuration problem into a per-request 500 with an upstream traceback in
    it. Returning None makes /api/chat answer 503 with "LLM not configured" and
    /health's key_present explain why -- and it lets the test suite run the whole
    HTTP contract with no credentials at all.
    """
    if not settings.llm_api_key:
        return None
    model = make_chat_model(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_chat_model,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
    )
    # Binding happens here, at the composition root, not inside the agent node.
    # Which tools a model can see is policy, and D4 will bind a different set
    # per path. A node that binds its own tools cannot be reused across paths.
    return build_graph(model=model.bind_tools(TOOLS), tools=TOOLS, checkpointer=checkpointer)


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
    app.state.checkpointer = await make_checkpointer(
        settings.checkpoint_backend, settings.sqlite_path, settings.database_url
    )
    app.state.graph = _build_graph(app.state.checkpointer)
    app.state.tracer = make_tracer(
        public_key=settings.langfuse_public_key,
        secret_key=(
            settings.langfuse_secret_key.get_secret_value()
            if settings.langfuse_secret_key
            else None
        ),
        host=settings.langfuse_host,
        environment=settings.app_env,
    )
    # Off the startup path on purpose. auth_check() is a blocking call to a third
    # party; awaiting it here would mean a slow or unreachable Langfuse delays
    # every `uvicorn --reload`. The answer is worth exactly one log line, so it
    # arrives in the background. The task is kept on app.state because an
    # unreferenced task can be garbage collected mid-flight.
    app.state.tracer_check = (
        asyncio.create_task(_report_tracer_auth(app.state.tracer))
        if app.state.tracer is not None
        else None
    )
    yield
    if app.state.tracer_check is not None:
        app.state.tracer_check.cancel()
    shutdown_tracer(app.state.tracer)
    # The sqlite saver owns an aiosqlite connection, and aiosqlite runs it on a
    # thread of its own. Closing it here is what keeps repeated app instances --
    # a test suite, uvicorn --reload -- from accumulating threads.
    conn = getattr(app.state.checkpointer, "conn", None)
    if conn is not None:
        await conn.close()


async def _report_tracer_auth(tracer: object) -> None:
    if await asyncio.to_thread(verify_tracer, tracer):
        logger.info("langfuse credentials accepted")
    else:
        logger.warning(
            "langfuse tracing is configured but not reporting; check the keys and "
            "that LANGFUSE_HOST matches the project's region"
        )


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
        # Same rule as llm: a presence flag, never a key. "The dashboard is
        # empty" is the first symptom of tracing being off, and this is the
        # fastest way to tell that apart from a wrong region or a bad key.
        "tracing": {
            "langfuse": getattr(app.state, "tracer", None) is not None,
            "host": settings.langfuse_host,
        },
    }
