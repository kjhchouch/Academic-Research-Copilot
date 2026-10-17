"""Tracing, local-first: no keys means no tracing, no network, no failure.

Langfuse is the only observability backend in Week 1. Like `storage.py`, the
rule is that the app must boot and serve traffic with zero external services
configured — tracing is a diagnostic, and a diagnostic that can take the product
down is worse than no diagnostic.

Three consequences that shape this module:

* Missing keys return `None`, and every caller treats `None` as "do nothing".
  Nothing here raises on a missing or wrong key.
* `auth_check()` is never called on the startup path. It is a blocking network
  call to a third party, and Langfuse being down must not stop the gateway from
  answering users. `verify_tracer()` exists for the one place that wants the
  answer anyway (a warning in the startup log) and is called off the event loop.
* `shutdown()` matters. The SDK batches spans and flushes on a timer, so a dev
  server killed after one request loses exactly the trace you were looking at.

Note on what gets recorded: a trace contains full prompts and completions, so
enabling Langfuse Cloud means user text leaves the machine. For a single-user
local dev setup that is the point. Before this serves anyone else it needs the
`mask=` hook on the client, which is why the constructor call is centralised
here rather than scattered across call sites.

    LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST

Langfuse 5.x, not 3.x. The old `CallbackHandler(public_key=..., secret_key=...,
host=...)` signature is gone: the handler takes no credentials and resolves a
process-wide client via `get_client()`, which is what `make_tracer()` registers.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Reserved metadata keys the LangChain integration reads. Anything else passed in
# config metadata becomes ordinary trace metadata, so these are the only names
# that carry special meaning -- and the reason they live here instead of being
# spelled out in the chat route.
SESSION_ID_KEY = "langfuse_session_id"
USER_ID_KEY = "langfuse_user_id"
TRACE_NAME_KEY = "langfuse_trace_name"
TAGS_KEY = "langfuse_tags"


def make_tracer(
    *,
    public_key: str | None,
    secret_key: str | None,
    host: str,
    environment: str = "development",
    sample_rate: float = 1.0,
) -> Any | None:
    """Initialise the process-wide Langfuse client, or return None.

    Constructing the client does no I/O — it wires up an OpenTelemetry provider
    and a batching exporter — so this is safe to call in a lifespan even when
    Langfuse is unreachable. Returns None without touching the network when
    either key is missing, which is the "tracing off" state.
    """
    if not public_key or not secret_key:
        logger.info("tracing disabled: LANGFUSE keys not configured")
        return None

    from langfuse import Langfuse

    client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        environment=environment,
        sample_rate=sample_rate,
    )
    logger.info("tracing enabled: langfuse environment=%s host=%s", environment, host)
    return client


def verify_tracer(client: Any) -> bool:
    """Blocking credential check. Never call this from the event loop.

    Kept separate from `make_tracer` so the startup path stays I/O-free; the
    caller decides whether the answer is worth a thread. A False here means the
    keys are wrong or the region is wrong — and a wrong region reports as
    "unauthorized", which is indistinguishable from a bad key unless you know to
    look at LANGFUSE_HOST.
    """
    try:
        return bool(client.auth_check())
    except Exception as exc:  # noqa: BLE001 - tracing must never break the app
        logger.warning("langfuse auth_check failed: %s", type(exc).__name__)
        return False


def tracer_callbacks(client: Any | None) -> list[Any]:
    """The LangChain callback list for one graph config: one handler, or nothing.

    Called per request, and a fresh handler per request is the deliberate choice.
    The handler keeps mutable per-run dicts (`_runs`, `_context_tokens`,
    `_run_states`); they are keyed by run id so concurrent requests would not
    actually collide, but "would not collide" is a claim about a third party's
    internals that a load test could falsify, and the alternative costs one small
    allocation.

    What is genuinely lost: `_pending_resume_trace_contexts`, which links a
    *resumed* LangGraph run back to the trace it came from, is per-instance. D3's
    graph has no interrupts, so nothing resumes. If Week 2 adds a
    human-in-the-loop interrupt, this needs to become one handler per thread_id
    instead of one per request — and conversation grouping keeps working either
    way, because that rides on `langfuse_session_id` rather than on the handler.
    """
    if client is None:
        return []

    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def trace_metadata(
    *,
    thread_id: str,
    name: str,
    user_id: str | None = None,
    tags: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the config `metadata` dict that names and groups a trace.

    `thread_id` is passed twice, and both matter. Plain `thread_id` is what the
    LangChain integration uses as the LangGraph resume key, so consecutive turns
    of one conversation attach to one trace tree. `langfuse_session_id` is what
    the Langfuse UI groups by, so a session is browsable as a conversation
    instead of a pile of unrelated runs.

    Everything in `extra` lands as ordinary trace metadata — which is where
    ui_context goes, since those are the routing features worth correlating
    against later. Reserved keys are written last, so a caller cannot shadow the
    resume key by passing `thread_id=` as an extra.
    """
    metadata: dict[str, Any] = dict(extra)
    if user_id:
        metadata[USER_ID_KEY] = user_id
    if tags:
        metadata[TAGS_KEY] = tags
    metadata.update(
        {
            "thread_id": thread_id,
            SESSION_ID_KEY: thread_id,
            TRACE_NAME_KEY: name,
        }
    )
    return metadata


def shutdown_tracer(client: Any | None) -> None:
    """Flush and release. Safe to call with None, and safe to call twice."""
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown must not mask the real error
        logger.warning("langfuse shutdown failed: %s", type(exc).__name__)
