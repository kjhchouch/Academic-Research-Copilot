import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from paper_agent.citations import bind_citations
from paper_agent.graph import build_graph
from paper_agent.llm import make_chat_model
from paper_agent.observability import make_tracer, shutdown_tracer, verify_tracer
from paper_agent.routing import make_router
from paper_agent.storage import make_blob_store, make_cache, make_checkpointer
from paper_agent.tools import (
    TOOLS,
    MCPBridgeError,
    MCPToolBridge,
    StdioServerSpec,
    assert_unique_tool_names,
)

from api.config import Settings, get_settings
from api.routes import chat

settings = get_settings()
logger = logging.getLogger(__name__)

# `python -m <package>` rather than the `literature-search` console script: the
# script only works if the venv's Scripts/ (or bin/) is on PATH, which is not true
# of every way this gateway gets launched, and the failure would be a confusing
# spawn error at startup. sys.executable is the interpreter already running us, so
# it is also the one that has the workspace's editable installs.
LITERATURE_SERVER_NAME = "literature-search"
LITERATURE_SERVER_MODULE = "mcp_literature_search"


def _literature_spec(settings: Settings) -> StdioServerSpec:
    """The child's command line and its entire environment.

    Secrets are unwrapped here and nowhere else. Once they are plain strings in a
    dict they could in principle be logged, so the dict is built at the point of
    use and never passed anywhere that reports on itself -- `describe()` returns
    the server name and the tool list, not the env.
    """
    env: dict[str, str] = {}
    if settings.openalex_mailto:
        env["OPENALEX_MAILTO"] = settings.openalex_mailto
    if settings.openalex_api_key:
        env["OPENALEX_API_KEY"] = settings.openalex_api_key.get_secret_value()
    if settings.semantic_scholar_api_key:
        env["SEMANTIC_SCHOLAR_API_KEY"] = settings.semantic_scholar_api_key.get_secret_value()
    return StdioServerSpec(
        name=LITERATURE_SERVER_NAME,
        command=sys.executable,
        args=["-m", LITERATURE_SERVER_MODULE],
        env=env,
    )


def _mcp_health(app: FastAPI) -> dict[str, object]:
    """/health's tool-server block.

    Reports the same shape whether or not a bridge exists, so a client can read
    `mcp.connected` without first working out whether the server was enabled. The
    attribute is read defensively because /health answers before the lifespan has
    run in a bare TestClient -- which is most of the api test suite.
    """
    bridge = getattr(app.state, "mcp", None)
    described = (
        bridge.describe()
        if bridge is not None
        else {"server": LITERATURE_SERVER_NAME, "connected": False, "tools": []}
    )
    return {"enabled": settings.literature_mcp_enabled, **described}


async def _connect_literature(settings: Settings) -> MCPToolBridge | None:
    """Connect the retrieval server, or return None and carry on without it.

    None is a degraded-but-running outcome, not a swallowed error, and it follows
    the rule the tracer already set: the product boots with zero external
    services. Retrieval is the one capability that genuinely needs the network,
    so it is also the one that should not be able to take the gateway down -- a
    spawn failure here would otherwise mean no chat at all, including for the
    tools that work fine offline. The warning and /health's `mcp` block are how
    that degradation stays visible instead of looking like a model that refuses to
    search.
    """
    if not settings.literature_mcp_enabled:
        logger.info("literature MCP server disabled by configuration")
        return None
    bridge = MCPToolBridge(_literature_spec(settings))
    try:
        return await bridge.connect()
    except MCPBridgeError:
        # connect() already logged the traceback; this line is the operator's
        # summary, and it names the consequence rather than repeating the cause.
        logger.warning(
            "continuing without literature tools; the agent will answer from the "
            "model alone and should say so"
        )
        return None


def _build_router(settings: Settings, cache: object) -> object | None:
    """Compose the D4 routing cascade, or None when no backend is configured.

    None means `build_graph` gets `router=None` and compiles the bare ReAct loop
    with no routing preamble -- the product still answers, it just does not gate
    or classify. That is the local-first rule the tracer and checkpointer already
    follow: boot with zero external services.

    The fallback is the cheap chat model, not the reasoning one -- routing is a
    small structured classification, and `llm_chat_model` is what the config
    already designates for "the cheap graph nodes". So whenever the agent itself
    can run (an `llm_api_key` exists), routing is on through the fallback alone;
    the Jev key only upgrades the primary tier, and its absence is not a reason to
    route nothing. The cache is shared with the rest of the app, which is what
    makes a retry of the same message route identically (see `resilience.py`).
    """
    if not settings.llm_api_key and not settings.typesafe_api_key:
        return None
    fallback_model = (
        None
        if not settings.llm_api_key
        else make_chat_model(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=settings.llm_chat_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
        )
    )
    return make_router(
        jev_api_key=(
            settings.typesafe_api_key.get_secret_value() if settings.typesafe_api_key else None
        ),
        jev_model=settings.decision_model_backend,
        fallback_model=fallback_model,
        cache=cache,
    )


def _build_graph(
    checkpointer: object, tools: list[object], router: object | None = None
) -> object | None:
    """Compile the Fast Path graph, or return None when there is no credential.

    None is a deliberate outcome, not a swallowed error. Building ChatOpenAI
    without a key produces an object that fails on first use, which turns a
    configuration problem into a per-request 500 with an upstream traceback in
    it. Returning None makes /api/chat answer 503 with "LLM not configured" and
    /health's key_present explain why -- and it lets the test suite run the whole
    HTTP contract with no credentials at all.

    `router` is threaded straight through to `build_graph`; when it is None the
    graph has no routing preamble (see `_build_router`).
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
    return build_graph(
        model=model.bind_tools(tools), tools=tools, checkpointer=checkpointer, router=router
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn configures only its own three loggers and never touches root, so our
    # records fall through to logging.lastResort -- level WARNING. The failure is
    # silent and asymmetric: INFO (the cost line, "langfuse credentials accepted")
    # vanishes while warnings still print. In lifespan, not at import, so that a
    # test importing api.main does not reconfigure root for the whole pytest run.
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
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
    # Connected before the graph is built, because the graph needs the tool list.
    # Held on app.state for one reason only: aclose() has to run in this same task,
    # which is the one that entered it (see MCPToolBridge's docstring).
    app.state.mcp = await _connect_literature(settings)
    tools = list(TOOLS)
    if app.state.mcp is not None:
        tools.extend(app.state.mcp.tools)
    # Checked at composition time rather than left to ToolNode, which indexes tools
    # by name and would let one source silently shadow the other. Raising here does
    # orphan the child for a moment, but a stdio server exits when its stdin
    # closes, which happens as soon as this process goes away.
    assert_unique_tool_names(tools)
    # Wrapped after the uniqueness check so the check runs on the real tool
    # objects, and before the graph is built so the model binds the wrapped ones.
    # The wrapper injects a gateway-assigned citation_id into literature results
    # before the model reads them; see paper_agent.citations for why the ids come
    # from here and not from the model.
    tools = bind_citations(tools)
    logger.info("agent tools: %s", [tool.name for tool in tools])
    # Built before the graph, which takes it as the routing preamble, and after the
    # cache, which it shares -- so a retried message routes identically instead of
    # re-asking a model. None here is a supported configuration, not a failure: it
    # means the bare ReAct loop with no gate (see _build_router).
    app.state.router = _build_router(settings, app.state.cache)
    logger.info("routing: %s", "on" if app.state.router is not None else "off (bare ReAct loop)")
    app.state.graph = _build_graph(app.state.checkpointer, tools, router=app.state.router)
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
    # First, because it is the only resource that is another process: closing it
    # ends the literature-search child, and nothing else in this shutdown depends on
    # a tool call succeeding.
    if app.state.mcp is not None:
        await app.state.mcp.aclose()
    # The router may hold a Jev client with an open socket. aclose is a no-op when
    # it is absent or has no backends (see ResilientRouter.aclose), so this is safe
    # whether routing is on, off, or never built because lifespan errored early.
    if getattr(app.state, "router", None) is not None:
        await app.state.router.aclose()
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
        # Presence flags again. "Is the boundary gate even on?" is the first
        # question when a request is or is not refused, and `configured` answers
        # it: a False here means the bare ReAct loop with no routing preamble, so
        # nothing is being classified or gated. Backend name and a key-presence
        # flag, never the key.
        "routing": {
            "configured": getattr(app.state, "router", None) is not None,
            "backend": settings.decision_model_backend,
            "jev_key_present": bool(settings.typesafe_api_key),
        },
        # Tool names and a connected flag, straight from describe(): no env, no
        # command line, no upstream URLs. "The agent never searches" is otherwise
        # indistinguishable from "the model chose not to", and this is the one
        # place that can tell them apart.
        "mcp": _mcp_health(app),
    }
