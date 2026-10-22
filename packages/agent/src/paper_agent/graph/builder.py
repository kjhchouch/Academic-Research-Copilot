"""Graph assembly.

What is built here is the Fast Path: a plain ReAct loop -- one `agent` node that
may emit tool calls, one `tools` node that executes them, and an edge back -- with
an optional routing preamble in front of it. When a router is supplied the entry
becomes `START -> route -> {agent | clarify | refuse}`; when it is not, the graph
is exactly the bare loop, `START -> agent`. The Deep Path -- Supervisor plus
Researcher / Drafter / Verifier / Editor -- is Week 2, and it will be a different
graph, not this one with more nodes. The preamble is not that Supervisor: it makes
one decision (proceed / ask / refuse) and then hands off, rather than orchestrating
sub-agents.

Three deliberate absences:

No LLM Supervisor. The `route` node does call a model-backed router, but the edge
out of it is `_guard_edge`, a pure function inspecting `state["guard"]`. The
decision is probabilistic; the *transition* is not. Once the router has written a
guard value -- cached, and graded against the eval set -- the same value always
takes the same edge, so every transition here is reproducible and scoreable. That
is the difference between this and a Supervisor that asks a model "what next?" on
every hop: `tools_condition` is likewise a pure function inspecting the last
message for tool_calls, and nothing asks a model which edge to take.

No boundary in the prompt. `refuse` and `clarify` are nodes, not instructions. A
system-prompt rule against ghostwriting is a suggestion a model can be talked out
of; a graph edge that routes 降重 to a fixed refusal string cannot. See
`routing/guard.py` for why the integrity half of that decision is deterministic.

No recursion guard inside the graph. The bound is `recursion_limit` in the invoke
config (see DEFAULT_RECURSION_LIMIT), which LangGraph enforces by raising
GraphRecursionError. That surfaces as an `error` SSE event rather than a silent
truncate -- a loop that stops quietly is indistinguishable from a model that
finished early.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from paper_agent.graph.nodes import make_agent_node, make_guard_node, make_route_node
from paper_agent.graph.state import AgentState
from paper_agent.prompts.system import FAST_PATH_SYSTEM_PROMPT
from paper_agent.routing.guard import CLARIFY_MESSAGE, REFUSE_MESSAGE

# agent -> tools -> agent is two steps per tool call, so 12 allows roughly five
# tool rounds plus the final answer. DeepSeek flash rarely needs more than two on
# this workload, and the whole point of a low bound is that a stuck loop costs
# 12 steps instead of the default 25.
DEFAULT_RECURSION_LIMIT = 12

# The guard values `_guard_edge` may return, and the keys of its mapping. Kept as
# a tuple so the edge can validate a state value against it instead of trusting
# that `route` always wrote a legal one.
_GUARD_EXITS = ("proceed", "clarify", "refuse")


def _guard_edge(state: AgentState) -> str:
    """Pure function: the exit named by `state["guard"]`, or `clarify`.

    `clarify` is the fallback for a guard that is missing or not one of the three
    -- which cannot happen when `route` ran, so it is purely defensive. It is the
    safe direction: a request we failed to classify is asked to rephrase, never
    silently run through the agent. Routing to `agent` on an unknown guard would
    turn a bookkeeping bug into a boundary bypass.
    """
    guard = state.get("guard")
    return guard if guard in _GUARD_EXITS else "clarify"


def build_graph(
    *,
    model: Any,
    tools: Sequence[Any],
    checkpointer: Any = None,
    system_prompt: str | None = FAST_PATH_SYSTEM_PROMPT,
    router: Any = None,
) -> Any:
    """Compile the Fast Path graph.

    `model` must already have tools bound. `tools` is passed separately because
    ToolNode needs the real tool objects to execute them -- binding only tells
    the model the schemas exist. Passing the two out of sync is the classic
    failure: the model calls a tool the node cannot run, and the error surfaces
    as a ToolMessage nobody asked for.

    `router` is optional and gates the whole preamble. With `router=None` the
    graph is the bare ReAct loop, which is both the local-first boot (no routing
    backend configured, so the product still answers) and what every pre-D4 test
    builds. With a router, `route` runs first and `guard` decides whether the loop
    runs at all. Typed `Any` for the same reason as `model`: the node only calls
    `await router.decide(...)`, and tests pass a scripted fake.
    """
    graph = StateGraph(AgentState)
    graph.add_node("agent", make_agent_node(model, system_prompt=system_prompt))
    graph.add_node("tools", ToolNode(tools))

    if router is None:
        graph.add_edge(START, "agent")
    else:
        graph.add_node("route", make_route_node(router))
        graph.add_node("refuse", make_guard_node(REFUSE_MESSAGE))
        graph.add_node("clarify", make_guard_node(CLARIFY_MESSAGE))
        graph.add_edge(START, "route")
        # A mapping dict, not a bare conditional: the guard names the exit, and
        # an unmapped value would raise at invoke time rather than pick a default.
        graph.add_conditional_edges(
            "route",
            _guard_edge,
            {"proceed": "agent", "clarify": "clarify", "refuse": "refuse"},
        )
        graph.add_edge("refuse", END)
        graph.add_edge("clarify", END)

    # tools_condition returns "tools" or END by inspecting the last AIMessage.
    # No mapping dict: this graph has one possible continuation and inventing a
    # second would be the Supervisor, which is a different graph.
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
