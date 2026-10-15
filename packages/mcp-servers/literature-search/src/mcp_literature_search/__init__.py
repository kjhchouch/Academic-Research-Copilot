"""MCP server exposing literature search over OpenAlex / Semantic Scholar.

D5 fills this in. Three tools:
  search_papers(query, year_from, limit)  -> OpenAlex
  get_paper(paper_id)                     -> Semantic Scholar
  format_citation(doi, style)             -> apa | ieee | gb-t-7714

WARNING: this server speaks MCP over stdio, so stdout is the JSON-RPC channel.
Any print() will corrupt the protocol stream and surface as an opaque parse
error on the client. All logging must go to stderr:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
"""
