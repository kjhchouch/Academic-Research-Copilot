"""Graph nodes.

One node so far. The shape matters more than the size: `make_agent_node` is a
factory returning a closure, not a module-level function, because the node needs
a model and LangGraph nodes take exactly one argument (the state). The factory is
the seam that lets tests pass a scripted model and production pass a real one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain_core.messages import SystemMessage

from paper_agent.graph.state import AgentState

# The bound model is a Runnable, not a BaseChatModel: production passes
# `ChatOpenAI(...).bind_tools(TOOLS)` and tests pass a scripted fake. Typing it
# as BaseChatModel would be a lie about what this node requires -- it only ever
# calls ainvoke.
AgentNode = Callable[[AgentState], Awaitable[dict[str, object]]]


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
