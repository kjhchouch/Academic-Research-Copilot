"""MCP server exposing literature search over OpenAlex / Semantic Scholar.

Three tools, all read-only:

  search_papers(query, year_from, limit, sort)  -> OpenAlex full-text search
  get_paper(paper_id)                           -> detail + bibliography, either upstream
  format_citation(doi, style)                   -> apa | ieee | gb-t-7714

Run it over stdio with `python -m mcp_literature_search` or the installed
`literature-search` script; `server.build_server()` returns the `MCPServer` so a
test can drive it in process.

WARNING: this server speaks MCP over stdio, so stdout is the JSON-RPC channel.
Any `print()` corrupts the protocol stream and surfaces on the client as an
opaque parse error. All logging goes to stderr -- `server.main()` configures that
before constructing the server, and nothing else in this package should.
"""

from __future__ import annotations

from mcp_literature_search.config import Settings
from mcp_literature_search.server import SERVER_INSTRUCTIONS, SERVER_NAME, build_server, main

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "Settings", "build_server", "main"]
