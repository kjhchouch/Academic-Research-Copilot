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
    event: route   data: {...}               the D4 routing decision for this turn
    event: tool    data: {...}               a tool call started or finished
    event: citation data: {...}              one paper the agent may cite as [n]
    event: grounding data: {...}             D9 per-citation verdicts for the answer
    event: done    data: {}                  stream finished cleanly
    event: error   data: {"message": "..."}  stream failed; no `done` follows

`route` arrives once per turn, before any `token`, and carries the routing
decision with a stable shape -- `intent`, `guard`, `needs_clarify`, `path_deep`,
`source`, `confidence`, `from_cache`, each present and null when unknown. `guard`
is the load-bearing one: `"refuse"` and `"clarify"` mean the agent never ran, and
the single `token` that follows is the fixed boundary or clarification message
(the guard nodes emit it as an ordinary assistant turn, so the frontend renders a
refusal exactly like a reply). `"proceed"` means the ReAct loop is running and
`token`/`tool`/`citation` follow as usual. It is emitted only when a router is
wired; a gateway with no routing backend configured sends no `route` event, and
the transport already ignores unknown event names either way.

`tool` carries `status: "start"` with `name`/`id`/`args`, or `status: "end"` with
`name`/`id`/`result`. `result` is a JSON value, not a JSON string — see
`_result_of`. `citation` carries one `paper_agent.citations.Citation`: the
gateway-assigned `citation_id` (the `[n]` anchor), the paper's metadata, and a
`span` that is always null in Week 1 and reserved for Week 2 grounding.

Why not just speak the SDK's protocol from Python: that format is an internal
detail of a TypeScript package and it changes between releases. Keeping the
wire format owned by this service means `apps/web` does the mapping once, in a
typed custom transport that the compiler checks against the installed SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from paper_agent.citations import Citation, CitationRegistry, bind_registry
from paper_agent.graph import DEFAULT_RECURSION_LIMIT
from paper_agent.knowledge import ComparisonReviewError, bind_knowledge, knowledge_guard
from paper_agent.observability import trace_metadata, tracer_callbacks
from paper_agent.pricing import UnknownModelError, cost_of, format_usd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from api.conversation_stream import record_stream
from api.research import build_comparison_context

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
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    use_knowledge: bool = False
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    knowledge_task: Literal["qa", "compare"] = "qa"

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _require_comparison_scope(self) -> ChatRequest:
        if self.knowledge_task == "compare":
            if not self.use_knowledge or not self.project_id or not self.thread_id:
                raise ValueError("资料对比必须启用资料问答并指定项目和已创建的会话")
            if (
                not 2 <= len(self.document_ids) <= 5
                or len(set(self.document_ids)) != len(self.document_ids)
                or any(not identifier.strip() for identifier in self.document_ids)
            ):
                raise ValueError("资料对比需要选择 2 至 5 份不重复的资料")
        return self

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


def _citation_payload(citation: Citation) -> dict[str, Any]:
    """A citation as a wire dict, with a stable shape.

    Every field is present and null when the upstream did not supply it --
    including `span`, always null in Week 1. A stable shape is what lets the
    frontend type the event once; dropping nulls would make `doi` sometimes
    absent and sometimes present, which is a different and worse contract than
    "doi is a string or null".
    """
    return citation.model_dump(mode="json")


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


def _route_event(update: dict[str, Any] | None) -> dict[str, str] | None:
    """The `route` node's state update as one `route` event, or None.

    Stable shape, every key present and null when the router did not supply it --
    the same rule as `_citation_payload`, so the frontend types the event once.
    Keyed off `guard` rather than `intent` because `guard` is the field the graph
    actually branched on; if it is absent this is not a route update at all.
    """
    fields = update or {}
    if "guard" not in fields:
        return None
    return _event(
        "route",
        {
            "intent": fields.get("intent"),
            "guard": fields.get("guard"),
            "needs_clarify": fields.get("needs_clarify"),
            "path_deep": fields.get("path_deep"),
            "source": fields.get("route_source"),
            "confidence": fields.get("route_confidence"),
            "from_cache": fields.get("route_from_cache"),
        },
    )


def _grounding_event(update: dict[str, Any] | None) -> dict[str, str] | None:
    """The `verify` node's state update as one `grounding` event, or None.

    Stable shape, same rule as `route` and `citation`: every field present, null
    when unknown. `source` names the entailment backend wired for this turn
    (null when none is, i.e. pure L0 marking) -- the same provenance discipline
    as the routing eval's `by_source`, so a degraded backend's marking can never
    be read as the production one's.

    Emitted once per turn, after the answer's tokens and before `done`. Absent
    entirely when the answer cited nothing -- that absence is the frontend's
    signal that there is nothing to mark, and it is also why turns without
    citations pay nothing for grounding.
    """
    fields = update or {}
    verdicts = fields.get("grounding")
    if not verdicts:
        return None
    return _event(
        "grounding",
        {
            "verdicts": [
                {
                    "citation_id": verdict.get("citation_id"),
                    "verdict": verdict.get("verdict"),
                    "reason": verdict.get("reason"),
                }
                for verdict in verdicts
            ],
            "source": fields.get("grounding_source"),
        },
    )


def _guard_text_events(update: dict[str, Any] | None) -> list[dict[str, str]]:
    """A `refuse`/`clarify` node's fixed message, streamed as `token` events.

    These nodes emit a plain `AIMessage`, not a model generation, so it never
    appears in `stream_mode="messages"` -- only here, in the node update. Routing
    it through the same `token` event as real model output is deliberate: to the
    user a refusal is just the assistant's reply, and the frontend already knows
    how to render `token`. Emitting a distinct event type would make the client
    special-case a message that is, on the wire, ordinary prose.
    """
    events: list[dict[str, str]] = []
    for message in (update or {}).get("messages", []):
        if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content:
            events.append(_event("token", {"text": message.content}))
    return events


def _public_error(exc: Exception) -> str:
    """A message safe to show a user, and useless to an attacker.

    Exception text from an HTTP client can contain the request URL, and from the
    OpenAI SDK it can contain the api key's last four characters or a echoed
    Authorization header. The detail goes to the log, never to the stream.
    """
    if isinstance(exc, ComparisonReviewError):
        # The cause may echo a private draft or review JSON; do not log its traceback.
        logger.warning("comparison evidence review failed")
        return "资料对比核验未通过，请重试或调整所选资料。"
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
    req: ChatRequest,
    agent: Any,
    thread_id: str,
    tracer: Any | None = None,
    knowledge: list[dict[str, Any]] | None = None,
    comparison: dict[str, Any] | None = None,
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
    registry = CitationRegistry()
    cited = 0
    knowledge_sent = False
    comparison_answer_sent = False
    query_guard = knowledge_guard(last_user_text(req.messages)) if knowledge is not None else None
    stream_guard = "proceed"

    def knowledge_events() -> list[dict[str, str]]:
        nonlocal knowledge_sent
        if knowledge_sent or knowledge is None or query_guard != "proceed":
            return []
        if stream_guard != "proceed":
            return []
        knowledge_sent = True
        events = [_event("knowledge-coverage", comparison)] if comparison is not None else []
        events.extend(_event("knowledge-source", source) for source in knowledge)
        return events

    try:
        # Bound for the whole stream so the citation wrappers deep inside the
        # graph's ToolNode -- which were built once in lifespan and cannot close
        # over per-request state -- find this turn's registry through the
        # ContextVar. Reset in the context manager's finally, including on error
        # or client abandonment.
        with bind_registry(registry), bind_knowledge(knowledge, comparison):
            async for mode, chunk_or_update in agent.astream(
                graph_input, config, stream_mode=["messages", "updates"]
            ):
                if mode == "messages":
                    message, meta = chunk_or_update
                    if isinstance(message, AIMessageChunk):
                        _record_usage(usage, message)
                        if (
                            comparison is None
                            and meta.get("langgraph_node", "agent") == "agent"
                            and isinstance(message.content, str)
                            and message.content
                        ):
                            # Comparison chunks are private draft/review output. Only
                            # the node's validated final update may publish its answer.
                            yield _event("token", {"text": message.content})
                else:
                    for node, update in chunk_or_update.items():
                        # `route` carries no messages and the guard nodes carry no
                        # tool calls, so these branches and `_tool_events` are
                        # mutually exclusive by node name -- one update is exactly
                        # one of them.
                        if node == "route":
                            stream_guard = (update or {}).get("guard", "clarify")
                            event = _route_event(update)
                            if event is not None:
                                yield event
                        elif node in ("refuse", "clarify"):
                            stream_guard = node
                            for event in _guard_text_events(update):
                                yield event
                        elif node == "agent" and knowledge is not None:
                            stream_guard = (update or {}).get("guard", stream_guard)
                            for event in knowledge_events():
                                yield event
                            if not knowledge or stream_guard != "proceed":
                                for event in _guard_text_events(update):
                                    yield event
                            elif comparison is not None and not comparison_answer_sent:
                                for event in _guard_text_events(update):
                                    comparison_answer_sent = True
                                    yield event
                        elif node == "verify":
                            # D9：核验节点只写 state，事件在 token 之后、done 之前
                            # 出现一次。答案没有引用时节点不写 state，这里自然是 None。
                            event = _grounding_event(update)
                            if event is not None:
                                yield event
                        for event in _tool_events(node, update):
                            yield event
                    # Drained after each node update, not once at the end: papers
                    # are registered while the tools node runs, and emitting them
                    # as they appear lets the frontend build its reference panel
                    # progressively instead of all at once on `done`. `cited`
                    # deduplicates across updates because the registry list only
                    # ever grows, in assignment order.
                    new = registry.citations[cited:]
                    for citation in new:
                        yield _event("citation", _citation_payload(citation))
                    cited += len(new)
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
    request: Request,
    agent: Any = Depends(get_agent),
    tracer: Any | None = Depends(get_tracer),
) -> EventSourceResponse:
    thread_id = req.thread_id or uuid.uuid4().hex
    store = getattr(request.app.state, "workspace", None)
    if req.project_id is None:
        if req.use_knowledge or req.document_ids:
            raise HTTPException(422, "资料问答必须指定研究项目")
        if thread_id.startswith("project:") or (
            store is not None and await asyncio.to_thread(store.session_exists_anywhere, thread_id)
        ):
            raise HTTPException(404, "会话不存在于此范围")
        return EventSourceResponse(_agent_stream(req, agent, thread_id, tracer))

    if store is None:
        raise HTTPException(503, "工作区存储尚未就绪")
    if req.thread_id is None:
        raise HTTPException(422, "请先创建项目会话")
    project_id = req.project_id
    try:
        await asyncio.to_thread(store.get_session, project_id, thread_id)
        documents = [
            await asyncio.to_thread(store.get_document, project_id, document_id)
            for document_id in req.document_ids
        ]
        if req.knowledge_task == "compare" and any(d["status"] != "ready" for d in documents):
            raise HTTPException(422, "资料对比仅支持解析就绪的资料")
        saved = await asyncio.to_thread(store.session_messages, project_id, thread_id)
    except KeyError as exc:
        raise HTTPException(404, "项目、会话或资料不存在于当前范围") from exc

    newest = next(message for message in reversed(req.messages) if message.role == "user")
    if any(message["id"] == newest.id for message in saved):
        raise HTTPException(409, "此消息已经保存，请发送一条新消息")
    active = getattr(request.app.state, "active_sessions", None)
    if active is None:
        active = request.app.state.active_sessions = set()
    if thread_id in active:
        raise HTTPException(409, "此会话正在生成回答，请等待完成或停止后再试")
    active.add(thread_id)
    try:
        sources = None
        comparison = None
        if req.knowledge_task == "compare":
            try:
                sources, comparison = await asyncio.to_thread(
                    build_comparison_context,
                    store,
                    project_id,
                    last_user_text(req.messages),
                    req.document_ids,
                )
            except ValueError as exc:
                raise HTTPException(422, "资料对比需要 2 至 5 份不重复且解析就绪的资料") from exc
        elif req.use_knowledge:
            found = await asyncio.to_thread(
                store.search,
                project_id,
                last_user_text(req.messages)[:2000],
                req.document_ids or None,
            )
            sources = [
                {**chunk, "project_id": project_id, "source_id": f"K{index}"}
                for index, chunk in enumerate(found, 1)
            ]
        server_req = req.model_copy(
            update={"messages": [*[UIMessage.model_validate(message) for message in saved], newest]}
        )
        # Project content must not enter the external trace or a legacy thread namespace.
        events = _agent_stream(
            server_req,
            agent,
            f"project:{project_id}:{thread_id}",
            knowledge=sources,
            comparison=comparison,
        )
        return EventSourceResponse(
            record_stream(events, store, project_id, thread_id, newest.model_dump(), active)
        )
    except KeyError as exc:
        active.discard(thread_id)
        raise HTTPException(404, "资料已删除，请重新选择") from exc
    except BaseException:
        active.discard(thread_id)
        raise
