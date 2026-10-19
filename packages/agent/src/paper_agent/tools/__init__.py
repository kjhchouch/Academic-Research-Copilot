"""LangChain tools, from both sides of the process boundary.

`common` holds the in-process ones: deterministic, offline, side-effect free.
`mcp` holds the bridge to servers that run elsewhere -- retrieval tools, which hit
the network and return untrusted text.

Rule of thumb: if a tool's "when to use" explanation exceeds three sentences, it
belongs in skills/ as a Skill, not here.
"""

from paper_agent.tools.common import TOOLS, count_words
from paper_agent.tools.mcp import (
    MCPBridgeError,
    MCPToolBridge,
    StdioServerSpec,
    assert_unique_tool_names,
)

__all__ = [
    "TOOLS",
    "MCPBridgeError",
    "MCPToolBridge",
    "StdioServerSpec",
    "assert_unique_tool_names",
    "count_words",
]
