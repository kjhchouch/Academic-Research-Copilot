"""Graph assembly.

What is built here is a plain ReAct loop: one `agent` node that may emit tool
calls, one `tools` node that executes them, and an edge back. This is the Fast
Path. The Deep Path -- Supervisor plus Researcher / Drafter / Verifier / Editor
-- is Week 2, and it will be a different graph, not this one with more nodes.

Two deliberate absences:

No LLM Supervisor. `tools_condition` is a pure function inspecting the last
message for tool_calls. Nothing here asks a model what to do next. A routing
decision made by a model is a probabilistic event; the same input can take
different edges on different runs, which makes it unmeasurable against a dataset.
Every transition in this graph is reproducible, so every one of them can be
scored.

No recursion guard inside the graph. The bound is `recursion_limit` in the invoke
config (see DEFAULT_RECURSION_LIMIT), which LangGraph enforces by raising
GraphRecursionError. That surfaces as an `error` SSE event rather than a silent
truncate -- a loop that stops quietly is indistinguishable from a model that
finished early.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.graph import START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from paper_agent.graph.nodes import make_agent_node
from paper_agent.graph.state import AgentState
from paper_agent.prompts.system import FAST_PATH_SYSTEM_PROMPT

# agent -> tools -> agent is two steps per tool call, so 12 allows roughly five
# tool rounds plus the final answer. DeepSeek flash rarely needs more than two on
# this workload, and the whole point of a low bound is that a stuck loop costs
# 12 steps instead of the default 25.
DEFAULT_RECURSION_LIMIT = 12


def build_graph(
    *,
    model: Any,
    tools: Sequence[Any],
    checkpointer: Any = None,
    system_prompt: str | None = FAST_PATH_SYSTEM_PROMPT,
) -> Any:
    """Compile the Fast Path graph.

    `model` must already have tools bound. `tools` is passed separately because
    ToolNode needs the real tool objects to execute them -- binding only tells
    the model the schemas exist. Passing the two out of sync is the classic
    failure: the model calls a tool the node cannot run, and the error surfaces
    as a ToolMessage nobody asked for.
    """
    graph = StateGraph(AgentState)
    graph.add_node("agent", make_agent_node(model, system_prompt=system_prompt))
    graph.add_node("tools", ToolNode(tools))

    graph.add_edge(START, "agent")
    # tools_condition returns "tools" or END by inspecting the last AIMessage.
    # No mapping dict: this graph has one possible continuation and inventing a
    # second would be the Supervisor, which is a different graph.
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
