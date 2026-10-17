"""LangGraph state definitions and graph construction.

Filled in on D3 (first agent) and extended in Week 2 (outline / evidence /
verdicts / retry_count).
"""

from paper_agent.graph.builder import DEFAULT_RECURSION_LIMIT, build_graph
from paper_agent.graph.nodes import make_agent_node
from paper_agent.graph.state import AgentState

__all__ = [
    "DEFAULT_RECURSION_LIMIT",
    "AgentState",
    "build_graph",
    "make_agent_node",
]
