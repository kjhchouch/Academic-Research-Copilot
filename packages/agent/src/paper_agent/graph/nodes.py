"""Graph nodes.

Three nodes, one shape. Each `make_*_node` is a factory returning a closure
rather than a module-level function, because a node needs something the graph does
not (a model, a router, a fixed message) and LangGraph nodes take exactly one
argument -- the state. The factory is the seam that lets a test pass a scripted
double and production pass the real thing.

The nodes:

    agent   the ReAct step. Calls the tool-bound model, returns its message.
    route   the D4 preamble. Turns the newest user turn into a `Decisions` plus an
            L0 `guard` verdict and writes them to state. Calls the router, never
            the chat model, and binds no tools.
    refuse / clarify
            terminal exits built by `make_guard_node`. Emit a fixed message and
            end the graph.

`route` and the guard exits do not call the chat model, and that is load-bearing:
a boundary that a model produced can be argued with by the next cleverly-worded
turn, whereas a fixed string returned by a node cannot. The routing decision is
still probabilistic -- but it happens in `route`, its output is cached and graded
by the eval, and the L0 half of `guard_from` overrides it deterministically. The
*transitions* out of `route` read `state["guard"]`, a plain value, so every edge
is reproducible even though the decision behind it was not.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from paper_agent.graph.state import AgentState
from paper_agent.routing.decision_model import RouteInput
from paper_agent.routing.guard import guard_from

# The bound model is a Runnable, not a BaseChatModel: production passes
# `ChatOpenAI(...).bind_tools(TOOLS)` and tests pass a scripted fake. Typing it
# as BaseChatModel would be a lie about what this node requires -- it only ever
# calls ainvoke.
AgentNode = Callable[[AgentState], Awaitable[dict[str, object]]]


def _last_human_text(messages: list[Any]) -> str:
    """The newest user turn's text, or "" if there is none.

    Scanned from the end because the routing decision is about what the user just
    asked, not the whole thread -- and on a warm thread the state carries the full
    checkpointed history, so the newest HumanMessage is the one that arrived this
    turn. Content is a str on our path (chat.py builds `HumanMessage(content=text)`),
    but LangChain permits a list of blocks, so the list case is flattened rather
    than trusted away.
    """
    for message in reversed(messages):
        if not isinstance(message, HumanMessage):
            continue
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return ""


def make_route_node(router: Any) -> AgentNode:
    """Build the `route` node around a router (a `ResilientRouter` in production).

    Typed as `Any` for the same reason as the model in `make_agent_node`: the node
    only calls `await router.decide(request)`, and a test passes a scripted fake
    that is not a `ResilientRouter`. Requiring the concrete type would force every
    test to build the real cascade -- breaker, cache, backends -- to exercise one
    edge.

    The router never raises (it degrades to `static_fallback()`), so this node
    cannot take the graph down on a routing outage. And because `guard_from` runs
    the L0 boundary check on the raw query *before* consulting the decision, the
    integrity boundary still refuses an explicit "降重" even when every backend is
    down and the decision is the static one.
    """

    async def route(state: AgentState) -> dict[str, object]:
        request = RouteInput(
            query=_last_human_text(list(state["messages"])),
            ui_context=state.get("ui_context") or {},
        )
        decisions = await router.decide(request)
        return {
            "intent": decisions.intent,
            "guard": guard_from(decisions, request),
            "needs_clarify": decisions.needs_clarify,
            "path_deep": decisions.path_deep,
            "route_source": decisions.source,
            "route_confidence": decisions.confidence,
            "route_from_cache": decisions.from_cache,
        }

    return route


def make_guard_node(message: str) -> AgentNode:
    """Build a terminal node that emits one fixed message and ends the graph.

    Used for both `refuse` and `clarify`; only the message differs. No model call,
    so the reply is byte-identical every time -- which is what makes the boundary
    auditable and the refusal unarguable. The message is returned as an AIMessage
    so `add_messages` records it in the thread: the conversation then shows the
    assistant declined, and a follow-up turn replays that refusal as real history
    instead of the model rediscovering the boundary from scratch.
    """

    async def emit(state: AgentState) -> dict[str, object]:
        return {"messages": [AIMessage(content=message)]}

    return emit


def make_agent_node(model: object, *, system_prompt: str | None = None) -> AgentNode:
    """Build the `agent` node around an already-tool-bound model.

    The binding happens outside, in build_graph's caller. Two reasons, one
    practical and one principled:

    Practical -- LangChain's built-in test doubles (`GenericFakeChatModel` and
    friends) all raise NotImplementedError from `bind_tools`, as of
    langchain-core 1.6.10. If this node called bind_tools itself, no fake model
    could drive it, and the loop could only be tested against a live API.

    Principled -- which tools a model can see is a policy decision, and policy
    belongs at the composition root, not buried in a node. D4 routes between the
    Fast Path and the Deep Path, and the two bind different tool sets to
    different models. A node that hardcodes its own binding cannot be reused
    across those paths.

    The system prompt is prepended at call time rather than stored in state.
    Storing it would write it into every checkpoint -- once per turn, forever --
    and would put mutable per-request text ahead of the stable prefix that
    prompt caching keys on.
    """
    prefix = [SystemMessage(content=system_prompt)] if system_prompt else []

    async def call_model(state: AgentState) -> dict[str, object]:
        response = await model.ainvoke(prefix + list(state["messages"]))  # type: ignore[attr-defined]
        return {"messages": [response]}

    return call_model
