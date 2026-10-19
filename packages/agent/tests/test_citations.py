"""Tests for the Citation contract and the id assignment that grounds it.

Three levels, cheapest first: the contract's shape, the registry's numbering and
dedup rules in isolation, and finally the whole point -- a wrapped tool running
inside a real compiled ToolNode, proving the id is injected into the result the
model reads and lands in the registry the gateway drains.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from paper_agent.citations import (
    Citation,
    CitationRegistry,
    SpanRef,
    bind_citations,
    bind_registry,
    current_registry,
)
from typing_extensions import TypedDict

SEARCH_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


def paper(title: str, doi: str | None = None, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"source": "openalex", "title": title, "authors": ["A"], "year": 2020}
    if doi is not None:
        base["doi"] = doi
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


def test_citation_shape_is_stable_and_span_is_present_but_null() -> None:
    """`span` is reserved for Week 2 but must be *present* now, so a frontend
    typed against this event does not change shape when grounding lands."""
    dumped = Citation(citation_id=1, title="T", doi="10.1/x").model_dump(mode="json")
    assert set(dumped) == {
        "citation_id",
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "abstract",
        "span",
    }
    assert dumped["span"] is None
    assert dumped["authors"] == []


def test_spanref_tolerates_the_fields_week_2_will_add() -> None:
    """extra='allow' is the reason SpanRef can grow without a breaking change."""
    ref = SpanRef(claim_id="c1", start=3, end=9, future_field="ok")  # type: ignore[call-arg]
    assert ref.model_dump()["future_field"] == "ok"


def test_citation_ignores_unknown_upstream_fields() -> None:
    """A Paper carries cited_by_count, is_retracted, volume, ... none of which
    belong on the wire. extra='ignore' drops them rather than forwarding them."""
    cit = Citation(citation_id=2, title="T", is_retracted=True, cited_by_count=99)  # type: ignore[call-arg]
    assert "is_retracted" not in cit.model_dump()


# --------------------------------------------------------------------------- #
# Numbering and dedup
# --------------------------------------------------------------------------- #


def test_ids_are_sequential_from_one_and_injected_in_place() -> None:
    registry = CitationRegistry()
    p1, p2 = paper("One", "10.1/a"), paper("Two", "10.1/b")
    assert registry.register(p1) == 1
    assert registry.register(p2) == 2
    # Injected, because this dict is what becomes the ToolMessage the model reads.
    assert p1["citation_id"] == 1
    assert p2["citation_id"] == 2
    assert [c.citation_id for c in registry.citations] == [1, 2]


def test_same_doi_reuses_one_id_across_calls() -> None:
    """The model searching twice and getting an overlapping paper must not see
    two numbers for one work -- that is how a reference list gets duplicates."""
    registry = CitationRegistry()
    assert registry.register(paper("X", "10.1/x")) == 1
    again = paper("X (relisted)", "10.1/x")
    assert registry.register(again) == 1
    assert again["citation_id"] == 1
    assert len(registry) == 1


def test_dedup_falls_back_to_title_when_there_is_no_identifier() -> None:
    registry = CitationRegistry()
    assert registry.register(paper("Same Title")) == 1
    assert registry.register(paper("  same title  ")) == 1
    assert len(registry) == 1


def test_a_second_search_continues_numbering_rather_than_restarting() -> None:
    """One flat number space across the whole turn, so the model's [n] and the
    frontend's chip agree without either knowing how many searches happened."""
    registry = CitationRegistry()
    registry.register_result({"papers": [paper(f"A{i}", f"10.1/a{i}") for i in range(5)]})
    registry.register_result({"papers": [paper("B", "10.1/b")]})
    assert registry.citations[-1].citation_id == 6


def test_a_paper_without_a_title_is_not_citable() -> None:
    """Returns 0 instead of raising: a malformed upstream record must not kill an
    otherwise good turn."""
    registry = CitationRegistry()
    assert registry.register({"source": "openalex"}) == 0
    assert registry.register("not a dict") == 0
    assert len(registry) == 0


# --------------------------------------------------------------------------- #
# The three D5 result shapes, matched structurally
# --------------------------------------------------------------------------- #


def test_register_result_reads_a_search_papers_list() -> None:
    registry = CitationRegistry()
    result = {"query": "q", "count": 2, "papers": [paper("A", "10.1/a"), paper("B", "10.1/b")]}
    registry.register_result(result)
    assert [c.title for c in registry.citations] == ["A", "B"]
    assert result["papers"][0]["citation_id"] == 1


def test_register_result_reads_a_format_citation_record() -> None:
    registry = CitationRegistry()
    result = {"style": "apa", "citation": "…", "record": paper("R", "10.1/r")}
    registry.register_result(result)
    assert registry.citations[0].title == "R"
    assert result["record"]["citation_id"] == 1


def test_register_result_reads_a_bare_get_paper_result() -> None:
    registry = CitationRegistry()
    result = paper("Detail", "10.1/d", references=[], referenced_works_count=0)
    registry.register_result(result)
    assert registry.citations[0].title == "Detail"
    assert result["citation_id"] == 1


def test_register_result_ignores_a_non_paper_tool() -> None:
    """count_words returns no papers, so it must pass through untouched."""
    registry = CitationRegistry()
    result = {"text": "hi", "word_count": 1}
    registry.register_result(result)
    assert result == {"text": "hi", "word_count": 1}
    assert len(registry) == 0
    registry.register_result("plain string")  # must not raise


def test_a_paper_shared_between_search_and_get_paper_keeps_one_id() -> None:
    registry = CitationRegistry()
    registry.register_result({"papers": [paper("A", "10.1/a")]})
    detail = paper("A", "10.1/a", references=[])
    registry.register_result(detail)
    assert detail["citation_id"] == 1
    assert len(registry) == 1


# --------------------------------------------------------------------------- #
# The ContextVar seam
# --------------------------------------------------------------------------- #


def test_bind_registry_scopes_and_resets() -> None:
    assert current_registry() is None
    registry = CitationRegistry()
    with bind_registry(registry) as bound:
        assert bound is registry
        assert current_registry() is registry
    assert current_registry() is None


def test_bind_registry_resets_even_when_the_body_raises() -> None:
    registry = CitationRegistry()
    with pytest.raises(RuntimeError), bind_registry(registry):
        raise RuntimeError("boom")
    assert current_registry() is None


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #


@tool
def count_words(text: str) -> dict[str, Any]:
    """数一下字数。"""
    return {"text": text, "word_count": len(text.split())}


def async_search_tool(result: dict[str, Any]) -> StructuredTool:
    async def call(query: str) -> dict[str, Any]:
        return result

    return StructuredTool(
        name="search_papers", description="检索", coroutine=call, args_schema=SEARCH_ARGS
    )


def test_bind_citations_passes_sync_tools_through_unchanged() -> None:
    """count_words has no coroutine and returns no papers; wrapping it would only
    add a layer for nothing."""
    wrapped = bind_citations([count_words])
    assert wrapped[0] is count_words


async def test_wrapped_tool_injects_ids_into_the_result_and_the_registry() -> None:
    result = {"papers": [paper("A", "10.1/a"), paper("B", "10.1/b")]}
    (wrapped,) = bind_citations([async_search_tool(result)])
    registry = CitationRegistry()
    with bind_registry(registry):
        returned = await wrapped.coroutine(query="q")
    assert returned["papers"][0]["citation_id"] == 1
    assert [c.title for c in registry.citations] == ["A", "B"]


async def test_wrapped_tool_is_a_noop_without_a_bound_registry() -> None:
    """A tool run outside a chat turn -- or in a test that never bound one -- must
    still work, just without citation ids. Citations enhance the stream; they are
    never a precondition for a tool to return."""
    result = {"papers": [paper("A", "10.1/a")]}
    (wrapped,) = bind_citations([async_search_tool(result)])
    returned = await wrapped.coroutine(query="q")
    assert "citation_id" not in returned["papers"][0]


def test_wrapped_tool_preserves_the_openai_facing_schema() -> None:
    (wrapped,) = bind_citations([async_search_tool({"papers": []})])
    schema = convert_to_openai_tool(wrapped)
    assert schema["function"]["name"] == "search_papers"
    assert schema["function"]["parameters"]["required"] == ["query"]


# --------------------------------------------------------------------------- #
# The whole point: through a real compiled graph
# --------------------------------------------------------------------------- #


class _State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _tool_graph(tools: list[Any]) -> Any:
    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


async def test_ids_reach_the_model_and_the_gateway_through_a_real_toolnode() -> None:
    """The integration that matters. The tool runs inside a compiled ToolNode --
    a sub-task with a copied context -- and still mutates the registry the caller
    bound, and the citation_id still lands in the ToolMessage content the model
    reads on the next turn. If ContextVar propagation through the graph broke,
    this is where it would show."""
    result = {"papers": [paper("A", "10.1/a"), paper("B", "10.1/b")]}
    (wrapped,) = bind_citations([async_search_tool(result)])
    graph = _tool_graph([wrapped])
    call = AIMessage(
        content="", tool_calls=[{"name": "search_papers", "args": {"query": "q"}, "id": "c1"}]
    )

    registry = CitationRegistry()
    with bind_registry(registry):
        out = await graph.ainvoke({"messages": [call]})

    tool_message = next(m for m in out["messages"] if isinstance(m, ToolMessage))
    payload = json.loads(tool_message.content)
    assert [p["citation_id"] for p in payload["papers"]] == [1, 2]
    assert [c.title for c in registry.citations] == ["A", "B"]
