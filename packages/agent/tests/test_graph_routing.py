"""Graph-level tests for the D4 routing preamble.

Builds the real compiled graph with a `ScriptedRouter` and a `ScriptedChatModel`,
so the `route` node, the `guard` edge and the two terminal exits are exercised end
to end -- but no live decision service and no LLM call. This is the layer above
`test_routing_guard.py`: there the verdict is a pure function; here it has to
actually steer the graph and land the decision in state.

`asyncio_mode = "auto"`, so the async tests need no marker.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from paper_agent.graph import build_graph
from paper_agent.routing.decision_model import Decisions
from paper_agent.routing.guard import CLARIFY_MESSAGE, REFUSE_MESSAGE
from paper_agent.testing import ScriptedChatModel, ScriptedRouter

AGENT_REPLY = "agent ran"


def _decisions(
    intent: str, *, needs_clarify: bool = False, path_deep: bool = False, source: str = "jev"
) -> Decisions:
    return Decisions(
        intent=intent,  # type: ignore[arg-type]
        needs_clarify=needs_clarify,
        path_deep=path_deep,
        confidence=0.9,
        source=source,  # type: ignore[arg-type]
    )


def _graph(router: Any) -> Any:
    model = ScriptedChatModel(responses=[AIMessage(content=AGENT_REPLY)])
    return build_graph(
        model=model.bind_tools([]), tools=[], router=router, checkpointer=InMemorySaver()
    )


def _cfg(thread_id: str = "t") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def test_proceed_runs_the_agent_and_records_the_decision() -> None:
    router = ScriptedRouter(_decisions("polish"))
    out = await _graph(router).ainvoke({"messages": [("user", "润色这段")]}, _cfg())

    assert out["guard"] == "proceed"
    assert out["intent"] == "polish"
    assert out["route_source"] == "jev"
    assert out["route_confidence"] == 0.9
    assert out["messages"][-1].content == AGENT_REPLY  # the ReAct loop actually ran
    # The route node built RouteInput from the newest user turn.
    assert router.calls[0].query == "润色这段"


async def test_refuse_exits_with_the_boundary_message_and_skips_the_agent() -> None:
    """The model leaks (calls 降重 a `polish`); L0 still refuses. The agent never
    runs, so no amount of model error can produce ghostwritten output here."""
    router = ScriptedRouter(_decisions("polish"))
    out = await _graph(router).ainvoke({"messages": [("user", "帮我把这段降重")]}, _cfg())

    assert out["guard"] == "refuse"
    assert out["messages"][-1].content == REFUSE_MESSAGE
    assert all(message.content != AGENT_REPLY for message in out["messages"])


async def test_clarify_exits_with_the_clarification_message() -> None:
    router = ScriptedRouter(_decisions("unclear", needs_clarify=True, source="static"))
    out = await _graph(router).ainvoke({"messages": [("user", "在吗")]}, _cfg())

    assert out["guard"] == "clarify"
    assert out["messages"][-1].content == CLARIFY_MESSAGE
    assert all(message.content != AGENT_REPLY for message in out["messages"])


async def test_route_node_forwards_ui_context_into_routeinput() -> None:
    """`ui_context` is a stronger routing feature than the message text, so the
    route node must pass it through -- a selection is what turns "改一下" from
    unclear into polish."""
    router = ScriptedRouter(_decisions("polish"))
    await _graph(router).ainvoke(
        {
            "messages": [("user", "改一下")],
            "ui_context": {"selected_text": "在这项研究当中……", "active_panel": "editor"},
        },
        _cfg(),
    )
    assert router.calls[0].ui_context.get("selected_text") == "在这项研究当中……"
    assert router.calls[0].ui_context.get("active_panel") == "editor"


async def test_refusal_is_recorded_in_the_thread_history() -> None:
    """The guard exit writes its message to state, so the thread shows the
    assistant declined. A follow-up turn replays that refusal as real history
    rather than the model rediscovering the boundary."""
    graph = _graph(ScriptedRouter(_decisions("polish")))
    out = await graph.ainvoke({"messages": [("user", "帮我降重")]}, _cfg("persist"))
    assert out["messages"][-1].content == REFUSE_MESSAGE

    snapshot = await graph.aget_state(_cfg("persist"))
    assert snapshot.values["messages"][-1].content == REFUSE_MESSAGE
    assert snapshot.values["guard"] == "refuse"


async def test_graph_without_a_router_is_the_bare_react_loop() -> None:
    """No router, no gate: even a boundary query runs the agent. This is the
    local-first boot with no backend configured -- the preamble exists only when a
    router is wired, and every pre-D4 test builds the graph this way."""
    model = ScriptedChatModel(responses=[AIMessage(content=AGENT_REPLY)])
    graph = build_graph(model=model.bind_tools([]), tools=[], checkpointer=InMemorySaver())
    out = await graph.ainvoke({"messages": [("user", "帮我把这段降重")]}, _cfg())

    assert "guard" not in out
    assert out["messages"][-1].content == AGENT_REPLY
