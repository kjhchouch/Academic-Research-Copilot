"""The `Citation` contract, and the id assignment that grounds it.

This is the shape both sides of the wire agree on: the gateway emits `citation`
SSE events carrying it, and `apps/web` renders a `[n]` chip from it. It is
deliberately a separate module from the chat route and from the MCP bridge,
because it is the one piece of D6 that outlives both -- Week 2's span-level
grounding rewrites how a citation is *verified*, not what a citation *is*.

Grounding by construction
-------------------------
A citation id is assigned here, in the gateway process, from a paper a tool
actually returned -- never by the model. The flow:

1. A literature tool returns papers. `bind_citations` wraps each tool so its
   result is passed through the per-request `CitationRegistry`.
2. The registry assigns the next sequential id to each new paper (deduplicated
   by DOI/OpenAlex id, so the same paper cited twice keeps one number) and
   *writes `citation_id` back into the result dict*.
3. Because the id is injected before the ToolMessage is built, the model sees it
   and copies it -- the system prompt tells it to cite the `citation_id` it was
   given. A `[7]` with no paper behind it simply has no registry entry, which is
   exactly the signal Week 2's grounding check consumes.

The registry is per-request and travels in a `ContextVar`, not on the graph
state: it is derived from tool results that are already checkpointed in the
message history, so persisting it too would be redundant, and a fresh registry
per turn means ids are stable within the one stream the frontend is rendering.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class SpanRef(BaseModel):
    """Where in the user's document a citation is anchored.

    A Week 2 placeholder, and the only field in the contract that is always None
    today. It exists so the shape does not change when span-level verification
    lands -- a frontend written against a `citation` event now will not have to
    learn a new field later. `extra="allow"` for the same reason: Week 2 can add
    the real anchors without a breaking change to anything shipped in Week 1.
    """

    model_config = ConfigDict(extra="allow")

    claim_id: str | None = None
    start: int | None = None
    end: int | None = None


class Citation(BaseModel):
    """One reference the agent is allowed to cite, and nothing it invented.

    Mirrors the D5 `Paper` fields the product surfaces, trimmed to what a
    reference chip and panel need. `abstract` is included because the hover
    preview shows it; it is the one heavy field, and it is optional so a paper
    the upstream returned without an abstract still cites cleanly.
    """

    model_config = ConfigDict(extra="ignore")

    citation_id: int
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    abstract: str | None = None
    span: SpanRef | None = None


_CURRENT: contextvars.ContextVar[CitationRegistry | None] = contextvars.ContextVar(
    "paper_agent_citation_registry", default=None
)


def current_registry() -> CitationRegistry | None:
    """The registry bound to this request, or None when citations are off."""
    return _CURRENT.get()


@contextlib.contextmanager
def bind_registry(registry: CitationRegistry) -> Iterator[CitationRegistry]:
    """Bind `registry` for the duration of one streamed turn.

    A context manager rather than a bare set/reset so the token is always reset,
    including when the stream raises. ContextVars propagate into the graph's
    ToolNode coroutine and the value is this same object, so a wrapper deep
    inside the graph mutates the registry the caller reads back afterwards.
    """
    token = _CURRENT.set(registry)
    try:
        yield registry
    finally:
        _CURRENT.reset(token)


def _identity_of(paper: dict[str, Any]) -> str:
    """A stable dedup key for one paper, most-specific identifier first.

    Falls back to the title so a paper with no DOI or OpenAlex id still
    deduplicates against itself instead of getting two numbers. Lowercased and
    stripped because upstreams are inconsistent about title casing and a
    near-duplicate is worse than an occasional collision.
    """
    for key in ("doi", "openalex_id", "corpus_id"):
        value = paper.get(key)
        if value:
            return f"{key}:{value}"
    return f"title:{str(paper.get('title', '')).strip().lower()}"


def _paper_from(raw: dict[str, Any], citation_id: int) -> Citation:
    return Citation(
        citation_id=citation_id,
        title=str(raw.get("title", "")),
        authors=list(raw.get("authors") or []),
        year=raw.get("year"),
        venue=raw.get("venue"),
        doi=raw.get("doi"),
        abstract=raw.get("abstract"),
        span=None,
    )


class CitationRegistry:
    """Assigns and remembers the citation ids for one turn.

    Sequential from 1, in the order papers first appear across every tool call
    in the turn -- so a second `search_papers` continues at 6 rather than
    restarting at 1 and colliding with the first. That single flat number space
    is what lets the model's `[n]` and the frontend's chip agree without either
    side knowing how many searches happened.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._citations: list[Citation] = []

    @property
    def citations(self) -> list[Citation]:
        """All citations so far, in assignment order. A copy, so a caller cannot
        mutate the registry's own list while draining it."""
        return list(self._citations)

    def __len__(self) -> int:
        return len(self._citations)

    def register(self, paper: dict[str, Any]) -> int:
        """Assign (or reuse) an id for one paper dict, injecting `citation_id`
        into it in place so the model reads back the number it must cite."""
        if not isinstance(paper, dict) or not paper.get("title"):
            # Not a citable paper. Returning 0 rather than raising keeps a
            # malformed upstream record from killing an otherwise good turn.
            return 0
        key = _identity_of(paper)
        existing = self._by_key.get(key)
        if existing is not None:
            paper["citation_id"] = existing
            return existing
        citation_id = len(self._citations) + 1
        self._by_key[key] = citation_id
        self._citations.append(_paper_from(paper, citation_id))
        paper["citation_id"] = citation_id
        return citation_id

    def register_result(self, result: Any) -> None:
        """Pull every paper out of one tool result and register it.

        Knows the three D5 result shapes without importing them -- the server is
        a separate process and its models are not a dependency here, so the
        shapes are matched structurally: a `papers` list (search_papers), a
        `record` dict (format_citation), or a result that *is* a paper
        (get_paper). A result that matches none of these is left untouched, which
        is how a non-literature tool like count_words passes through.
        """
        if not isinstance(result, dict):
            return
        papers: list[dict[str, Any]] = []
        found_list = result.get("papers")
        if isinstance(found_list, list):
            papers.extend(p for p in found_list if isinstance(p, dict))
        record = result.get("record")
        if isinstance(record, dict):
            papers.append(record)
        if "title" in result and ("source" in result or "doi" in result):
            papers.append(result)
        for paper in papers:
            self.register(paper)


def bind_citations(tools: Sequence[BaseTool]) -> list[BaseTool]:
    """Wrap async tools so their results register citations before the model
    sees them.

    Only tools with a coroutine are wrapped: every MCP-bridged tool is async and
    every one of them can return papers, while the in-process sync tools
    (count_words) return no papers and are passed through untouched. The wrapper
    reads the registry from the ContextVar at call time, so one wrapped tool
    serves every request and each request gets its own numbering.

    When no registry is bound -- a tool run outside a chat turn, or in a test
    that did not bind one -- the result is returned unchanged. Citations are an
    enhancement to the stream, never a precondition for a tool to work.
    """
    wrapped: list[BaseTool] = []
    for tool in tools:
        inner = tool.coroutine
        if inner is None:
            wrapped.append(tool)
            continue

        async def call(_inner: Any = inner, **arguments: Any) -> Any:  # noqa: B023 - _inner is bound per iteration, not closed over late
            result = await _inner(**arguments)
            registry = _CURRENT.get()
            if registry is not None:
                registry.register_result(result)
            return result

        wrapped.append(
            StructuredTool(
                name=tool.name,
                description=tool.description,
                coroutine=call,
                args_schema=tool.args_schema,
            )
        )
    return wrapped
