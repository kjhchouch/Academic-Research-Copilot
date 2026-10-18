"""Chat endpoint.

D1: echoed a fixed reply, to prove the browser -> FastAPI path works.
D2: SSE stream behind a fake generator, to prove the streaming pipeline end to
    end before anything expensive sat behind it.
D3 (here): the generator is the LangGraph agent's output stream.

Wire protocol
-------------
The gateway emits its own tiny event vocabulary rather than the Vercel AI SDK's
`UIMessageChunk` protocol:

    event: token   data: {"text": "..."}     one text delta
    event: tool    data: {...}               a tool call started or finished
    event: done    data: {}                  stream finished cleanly
    event: error   data: {"message": "..."}  stream failed; no `done` follows

`tool` carries `status: "start"` with `name`/`id`/`args`, or `status: "end"` with
`name`/`id`/`result`. `result` is a JSON value, not a JSON string — see
`_result_of`.

Why not just speak the SDK's protocol from Python: that format is an internal
detail of a TypeScript package and it changes between releases. Keeping the
wire format owned by this service means `apps/web` does the mapping once, in a
typed custom transport that the compiler checks against the installed SDK.

`route` and `citation` are not emitted yet and deliberately so. A protocol event
carrying placeholder data is worse than no event: the frontend cannot tell a
real value from a stub, and the first consumer written against the stub will
encode its fakeness. `route` arrives with the D4 cascade, `citation` with Week 2
grounding. The transport already ignores unknown event names, so adding them
later needs no frontend change.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from paper_agent.graph import DEFAULT_RECURSION_LIMIT
from paper_agent.observability import trace_metadata, tracer_callbacks
from paper_agent.pricing import UnknownModelError, cost_of, format_usd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])

# Longest single user turn we accept. The old D1 model enforced this on
# `message`; now that the transport sends a whole `messages` array the check
# moves onto the extracted text, which is what actually reaches the model.
MAX_USER_CHARS = 8000


class UIMessage(BaseModel):
    """One message in the Vercel AI SDK v5+ wire format.

    `parts` is left loosely typed on purpose. User turns carry
    `[{"type": "text", "text": ...}]`, but assistant turns can also carry
    reasoning, tool-call and custom parts. Validating those strictly would
    couple us to an SDK internal we do not own and break the moment a new part
    type appears in a replayed history.
    """

    id: str
    role: Literal["system", "user", "assistant"]
    parts: list[dict[str, Any]] = Field(default_factory=list)


class UiContext(BaseModel):
    """Signals from the editor. Stronger routing features than the text itself."""

    model_config = ConfigDict(extra="allow")

    active_panel: str | None = None
    cursor_section: str | None = None
    selected_text: str | None = None
    paper_stage: str | None = None


class ChatRequest(BaseModel):
    # The SDK names the conversation id `id`; we name it `thread_id` because
    # that is the exact key LangGraph's checkpointer wants. Aliasing here means
    # the checkpointer integration needed no request-model change.
    thread_id: str | None = Field(default=None, alias="id")
    messages: list[UIMessage] = Field(min_length=1)
    trigger: Literal["submit-message", "regenerate-message"] | None = None
    ui_context: UiContext | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _require_user_text(self) -> ChatRequest:
        """Reject a history with nothing to route on, with a 422 that says why."""
        text = last_user_text(self.messages)
        if not text:
            raise ValueError("messages contains no user text")
        if len(text) > MAX_USER_CHARS:
            raise ValueError(f"user message exceeds {MAX_USER_CHARS} characters")
        return self


def last_user_text(messages: list[UIMessage]) -> str:
    """Newest user turn's text, concatenated across its text parts."""
    for message in reversed(messages):
        if message.role != "user":
            continue
        text = _text_of(message)
        if text.strip():
            return text
    return ""


def _text_of(message: UIMessage) -> str:
    """Text parts only.

    User turns can carry file and custom parts; assistant turns carry reasoning
    and tool parts. Taking everything would feed the model its own scratch work.
    """
    return "".join(
        part.get("text", "")
        for part in message.parts
        if part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def to_langchain_messages(messages: list[UIMessage]) -> list[Any]:
    """Convert the SDK's wire format into LangChain messages.

    `role: "system"` is dropped, not forwarded. The system prompt belongs to the
    gateway (paper_agent.prompts.system); a client-supplied one would be an
    injection vector that also silently loses prompt-cache hits, since the cache
    keys on a stable prefix. Validation still accepts the role so a client that
    sends one gets a normal reply rather than a 422 it cannot interpret.

    The SDK's message id is carried across as the LangChain id. That is what
    makes `add_messages` idempotent: re-sent history replaces by id instead of
    appending, so a client replay and a checkpoint restore cannot double up.
    """
    out: list[Any] = []
    for message in messages:
        text = _text_of(message)
        if not text.strip():
            continue
        if message.role == "user":
            out.append(HumanMessage(content=text, id=message.id))
        elif message.role == "assistant":
            out.append(AIMessage(content=text, id=message.id))
    return out


def _event(name: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
    # ensure_ascii=False: the queries are Chinese, and \uXXXX escapes would
    # triple the byte count and make the stream unreadable in DevTools.
    return {"event": name, "data": json.dumps(payload or {}, ensure_ascii=False)}


def _tool_events(node: str, update: dict[str, Any] | None) -> list[dict[str, str]]:
    """Turn a node update into `tool` events.

    Start is detected on the agent node's AIMessage.tool_calls, end on the tools
    node's ToolMessages. Deriving both from `updates` rather than reaching for
    astream_events keeps one streaming primitive for the whole endpoint.
    """
    events: list[dict[str, str]] = []
    for message in (update or {}).get("messages", []):
        if node == "agent" and isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                events.append(
                    _event(
                        "tool",
                        {
                            "status": "start",
                            "name": call.get("name"),
                            "id": call.get("id"),
                            "args": call.get("args"),
                        },
                    )
                )
        elif node == "tools":
            events.append(
                _event(
                    "tool",
                    {
                        "status": "end",
                        "name": getattr(message, "name", None),
                        "id": getattr(message, "tool_call_id", None),
                        "result": _result_of(message.content),
                    },
                )
            )
    return events


def _result_of(content: Any) -> Any:
    """ToolMessage.content as a JSON value rather than a JSON string.

    LangChain stringifies whatever a tool returns, so without this the wire
    format nests a JSON document inside a JSON string and every consumer has to
    parse twice. Tools that return prose still work: the parse fails and the
    string passes through unchanged, so `result` is "a JSON value", not "an
    object".
    """
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except ValueError:
        return content


def _public_error(exc: Exception) -> str:
    """A message safe to show a user, and useless to an attacker.

    Exception text from an HTTP client can contain the request URL, and from the
    OpenAI SDK it can contain the api key's last four characters or a echoed
    Authorization header. The detail goes to the log, never to the stream.
    """
    name = type(exc).__name__
    if name == "GraphRecursionError":
        return "agent exceeded its step limit; the request was stopped"
    logger.exception("chat stream failed")
    return f"upstream error ({name})"


# message id -> (model name, usage_metadata). Keyed by id because one streamed
# generation arrives as many chunks that each repeat the cumulative total; the
# last write wins, which is the correct one.
type UsageByMessage = dict[str, tuple[str, dict[str, Any]]]


async def _agent_stream(
    req: ChatRequest, agent: Any, thread_id: str, tracer: Any | None = None
) -> AsyncIterator[dict[str, str]]:
    ui_context = (
        req.ui_context.model_dump(exclude_none=True) if req.ui_context is not None else None
    )
    config: dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }
    if tracer is not None:
        # Callbacks ride in the per-request config rather than being bound at
        # compile time: which trace a run belongs to is a property of the
        # request, and a graph compiled once in lifespan cannot know it.
        config["callbacks"] = tracer_callbacks(tracer)
        # Omitted entirely when absent rather than passed as None: Langfuse logs
        # "Propagated attribute 'metadata.ui_context' is None. Dropping value."
        # on every request that has no UI context, which is most of them.
        extras = {} if ui_context is None else {"ui_context": ui_context}
        config["metadata"] = trace_metadata(
            thread_id=thread_id,
            name="chat.fast_path",
            **extras,
        )

    # The client replays the whole history every turn and the checkpointer also
    # stores it. Forwarding both would double the context on every request, so
    # the server owns history and only the newest turn is sent. The exception is
    # a cold thread -- a restart with CHECKPOINT_BACKEND=memory, or a first
    # message -- where nothing is stored yet, so the client's history is the
    # only copy and has to be seeded in.
    history = to_langchain_messages(req.messages)
    try:
        snapshot = await agent.aget_state(config)
        existing = (snapshot.values or {}).get("messages") if snapshot else None
    except Exception:  # noqa: BLE001 - a cold backend must not 500 the request
        logger.warning("checkpoint read failed for thread %s; seeding from client", thread_id)
        existing = None

    graph_input: dict[str, Any] = {"messages": history if not existing else history[-1:]}
    if ui_context is not None:
        # Set unconditionally rather than only when the thread is cold: these
        # signals describe where the user is right now, and a cursor position
        # from three turns ago is actively misleading to the D4 cascade.
        graph_input["ui_context"] = ui_context

    usage: UsageByMessage = {}
    try:
        async for mode, chunk_or_update in agent.astream(
            graph_input, config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                message, _meta = chunk_or_update
                # Type, not node name. `stream_mode="messages"` also yields the
                # ToolMessage produced by the tools node, and without this filter
                # the tool's raw JSON is streamed to the user as if the model had
                # written it. Filtering on langgraph_node would work today and
                # break silently the first time a node is renamed.
                if isinstance(message, AIMessageChunk):
                    _record_usage(usage, message)
                    if isinstance(message.content, str) and message.content:
                        yield _event("token", {"text": message.content})
            else:
                for node, update in chunk_or_update.items():
                    for event in _tool_events(node, update):
                        yield event
        yield _event("done")
    except Exception as exc:  # noqa: BLE001 - the stream must close with a reason
        # No `done` after an `error`. The client treats a missing `done` as
        # truncation; sending both would make a failure look like a clean stop.
        yield _event("error", {"message": _public_error(exc)})
    finally:
        # In `finally` because a turn that errored out, or was abandoned by the
        # client, still spent tokens. That is precisely when the cost is most
        # worth seeing.
        _log_turn_cost(usage, thread_id)


def _record_usage(usage: UsageByMessage, message: AIMessageChunk) -> None:
    if not message.usage_metadata:
        return
    model = str((message.response_metadata or {}).get("model_name") or "")
    # `model_name` is what the provider says served the request, which is not
    # necessarily what we asked for -- DeepSeek accepts retired aliases and
    # routes them elsewhere. Pricing the requested name would hide that.
    if message.id is None:
        # Only reached if a provider streams no ids at all. Each usage-bearing
        # chunk then counts as its own generation, which over-counts if one
        # generation reports usage twice. Over-counting is the safe direction
        # for something whose job is to warn you about spend.
        usage[f"<no-id:{len(usage)}>"] = (model, dict(message.usage_metadata))
    else:
        usage[message.id] = (model, dict(message.usage_metadata))


def _log_turn_cost(usage: UsageByMessage, thread_id: str) -> None:
    """One log line per turn: what it cost, and what it was made of.

    Langfuse cannot answer this. Its managed price table has no DeepSeek entry,
    and even a hand-written one cannot express peak/off-peak rates -- so the
    dashboard shows $0.00 for real work. `paper_agent.pricing` explains both.

    Absent usage is reported as unknown rather than as zero, for the same
    reason: a cost of 0 must mean "this was free", never "we could not tell".
    """
    if not usage:
        logger.info("turn %s: provider reported no token usage; cost unknown", thread_id)
        return

    now = datetime.now(UTC)
    # Grouped by (model, peak) rather than listed per generation: a tool-calling
    # turn runs the same model twice, and "deepseek-flash=$0.00026,
    # deepseek-flash=$0.00011" reads like a bug. The peak flag stays in the key so
    # a turn straddling 04:00 or 10:00 UTC still reports its two rates separately.
    totals: dict[tuple[str, bool], Decimal] = {}
    unpriced = 0
    for model, meta in usage.values():
        try:
            cost = cost_of(model, meta, at=now)
        except UnknownModelError:
            logger.warning("turn %s: no price entry for model %r", thread_id, model)
            unpriced += 1
            continue
        key = (model or "<unknown>", cost.peak)
        totals[key] = totals.get(key, Decimal(0)) + cost.usd

    if not totals:
        logger.warning("turn %s: no priced model calls; cost unknown", thread_id)
        return
    total = sum(totals.values(), Decimal(0))
    parts = [
        f"{name}={format_usd(usd)}{'@peak' if peak else ''}" for (name, peak), usd in totals.items()
    ]
    suffix = f" + {unpriced} unpriced" if unpriced else ""
    logger.info("turn %s cost %s%s (%s)", thread_id, format_usd(total), suffix, ", ".join(parts))


def get_agent(request: Request) -> Any:
    """The compiled graph, built once in lifespan.

    A dependency rather than a module global so tests can override it with
    `app.dependency_overrides` and never construct a real ChatOpenAI -- which
    would need an API key the CI suite does not have.
    """
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        # 503, not 500: the service is up and correctly reporting that it cannot
        # serve this route yet. /health's llm.key_present says why.
        raise HTTPException(status_code=503, detail="agent unavailable: LLM not configured")
    return graph


def get_tracer(request: Request) -> Any | None:
    """The Langfuse client, or None when tracing is off.

    Unlike `get_agent` this never raises. A missing tracer is a supported
    configuration, not a degraded one: the whole point of the local-first rule is
    that the product works with zero external services, and tracing is the first
    thing that should be optional.
    """
    return getattr(request.app.state, "tracer", None)


@router.post("/chat")
async def chat_endpoint(
    req: ChatRequest,
    agent: Any = Depends(get_agent),
    tracer: Any | None = Depends(get_tracer),
) -> EventSourceResponse:
    # A thread without an id still needs one for the checkpointer. Ephemeral is
    # correct here, not a bug: an anonymous turn should not silently resume
    # somebody else's conversation.
    thread_id = req.thread_id or uuid.uuid4().hex
    return EventSourceResponse(_agent_stream(req, agent, thread_id, tracer))
