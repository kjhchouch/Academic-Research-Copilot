"""In-process LangChain tools.

Cross-process tools live in packages/mcp-servers/ and are reached over MCP.
Rule of thumb: if a tool's "when to use" explanation exceeds three sentences,
it belongs in skills/ as a Skill, not here.
"""
