"""The MCP surface: what a client sees, and what a model reads when things fail.

Driven through `mcp.client.Client(build_server(...))`, which connects in process --
no subprocess, no socket, no network, and therefore no budget spent. The transport
is injected so only the HTTP layer is a mock; everything above it (schema
generation, argument validation, `ToolError` conversion, structured output) is the
real SDK doing the real work. That is the point of testing at this level rather
than calling the tool functions directly: they are closures inside `build_server`,
and what an agent receives is the wire shape, not the return value.
"""

from __future__ import annotations

import json
from typing import NamedTuple

import httpx2
import pytest
from mcp.client import Client
from mcp.types import CallToolResult
from mcp_literature_search.config import Settings
from mcp_literature_search.server import (
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    build_server,
)

REAL_ID = "W3133849783"
REAL_DOI = "10.1109/icde53745.2022.00099"
DOI_PATH = f"/works/doi:{REAL_DOI}"


class Served(NamedTuple):
    """A connected client plus the requests its server made."""

    client: Client
    seen: list[httpx2.Request]


def serve(settings: Settings, routes: dict, make_transport) -> Served:
    transport, seen = make_transport(routes)
    return Served(Client(build_server(settings, transport=transport)), seen)


@pytest.fixture
def served(settings, search_payload, make_transport) -> Served:
    """A server that answers `/works` from the real captured fixture."""
    return serve(settings, {"/works": search_payload}, make_transport)


def dump(result: CallToolResult) -> str:
    """Everything a model could possibly read out of a result, as one string."""
    text = "\n".join(block.text for block in result.content if block.type == "text")
    structured = json.dumps(result.structured_content, ensure_ascii=False, sort_keys=True)
    return f"{text}\n{structured}"


# ---------------------------------------------------------------------------
# Handshake and tool listing
# ---------------------------------------------------------------------------


async def test_handshake_reports_the_server_identity(served) -> None:
    async with served.client as session:
        assert session.server_info.name == SERVER_NAME
        assert session.server_info.version == SERVER_VERSION
        assert session.instructions == SERVER_INSTRUCTIONS


async def test_instructions_state_the_trust_boundary() -> None:
    """Sent on connect, so this is the one place the boundary is always read."""
    assert "不可信" in SERVER_INSTRUCTIONS
    assert "不是给你的指令" in SERVER_INSTRUCTIONS
    assert "不代写" in SERVER_INSTRUCTIONS
    assert "不要重试" in SERVER_INSTRUCTIONS


async def test_exactly_three_tools(served) -> None:
    async with served.client as session:
        tools = (await session.list_tools()).tools
    assert [tool.name for tool in tools] == ["search_papers", "get_paper", "format_citation"]


async def test_every_tool_is_declared_read_only(served) -> None:
    """`read_only_hint` is what lets a client auto-approve instead of prompting."""
    async with served.client as session:
        tools = (await session.list_tools()).tools
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        # The honest half: these do leave the machine.
        assert tool.annotations.open_world_hint is True, tool.name


async def test_every_tool_is_described_in_chinese_for_the_model(served) -> None:
    async with served.client as session:
        tools = (await session.list_tools()).tools
    for tool in tools:
        assert tool.description and len(tool.description) > 40, tool.name
        assert any("\u4e00" <= char <= "\u9fff" for char in tool.description), tool.name


async def test_every_tool_publishes_an_output_schema(served) -> None:
    """Returning a pydantic model is what produces this; a bare dict would not."""
    async with served.client as session:
        tools = (await session.list_tools()).tools
    for tool in tools:
        assert tool.output_schema, tool.name
        assert tool.output_schema["type"] == "object"


async def test_search_input_schema_carries_the_bounds_and_the_defaults(served) -> None:
    async with served.client as session:
        tool = next(t for t in (await session.list_tools()).tools if t.name == "search_papers")
    schema = tool.input_schema
    assert schema["required"] == ["query"]
    assert schema["properties"]["limit"]["default"] == 5
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["sort"]["enum"] == ["relevance", "cited"]
    assert schema["properties"]["sort"]["default"] == "relevance"
    # Nullable, with the bounds on the integer arm rather than on the union.
    year = schema["properties"]["year_from"]["anyOf"]
    assert {"type": "null"} in year
    assert {"maximum": 2100, "minimum": 1400, "type": "integer"} in year


async def test_no_api_key_is_needed_to_serve(served) -> None:
    """`settings` has no keys at all; booting and listing tools must not require one."""
    async with served.client as session:
        assert len((await session.list_tools()).tools) == 3


# ---------------------------------------------------------------------------
# search_papers
# ---------------------------------------------------------------------------


async def test_search_papers_roundtrip(served, search_payload) -> None:
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "contrastive learning"})
    assert result.is_error is False
    content = result.structured_content
    assert content["query"] == "contrastive learning"
    assert content["sort"] == "relevance"
    assert content["count"] == 1
    assert content["truncated"] is False
    assert content["total_available"] == search_payload["meta"]["count"]

    paper = content["papers"][0]
    assert paper["title"] == "Contrastive Learning for Sequential Recommendation"
    assert paper["doi"] == REAL_DOI
    assert paper["openalex_id"] == REAL_ID
    assert paper["year"] == 2022
    assert paper["source"] == "openalex"
    assert paper["abstract"].startswith("Sequential recommendation methods play")
    assert served.seen[0].url.params["search"] == "contrastive learning"


async def test_the_text_block_and_the_structured_block_agree(served) -> None:
    """Clients without structured-output support read the text block instead."""
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "contrastive learning"})
    text = json.loads(result.content[0].text)
    assert text["papers"][0]["title"] == result.structured_content["papers"][0]["title"]


async def test_limit_above_the_ceiling_is_clamped_and_says_so(
    settings, search_payload, make_transport
) -> None:
    """The schema allows 100; the server's budget allows `max_results`."""
    served = serve(settings, {"/works": search_payload}, make_transport)
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q", "limit": 50})
    assert result.structured_content["truncated"] is True
    assert served.seen[0].url.params["per-page"] == str(settings.max_results)


async def test_limit_within_the_ceiling_is_not_flagged(served) -> None:
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q", "limit": 3})
    assert result.structured_content["truncated"] is False


async def test_truncated_is_not_just_there_are_more_matches(served) -> None:
    """A flag that is always on is a flag nobody reads; `total_available` carries that."""
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q", "limit": 1})
    content = result.structured_content
    assert content["truncated"] is False
    assert content["total_available"] == 4217397


async def test_year_from_becomes_a_date_filter(served) -> None:
    async with served.client as session:
        await session.call_tool("search_papers", {"query": "q", "year_from": 2020})
    assert served.seen[0].url.params["filter"] == "from_publication_date:2020-01-01"


async def test_sort_cited_is_passed_through(served) -> None:
    async with served.client as session:
        await session.call_tool("search_papers", {"query": "q", "sort": "cited"})
    assert served.seen[0].url.params["sort"] == "cited_by_count:desc"


async def test_an_out_of_range_argument_never_reaches_the_network(served) -> None:
    """Validated at the boundary, so a bad year costs nothing out of the budget."""
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q", "year_from": 1200})
    assert result.is_error is True
    assert served.seen == []


# ---------------------------------------------------------------------------
# Failure text
# ---------------------------------------------------------------------------


async def test_a_rate_limit_reaches_the_model_as_advice(
    settings, make_transport, rate_limited
) -> None:
    """The failure mode the whole design is built around: do not let it retry."""
    served = serve(settings, {"/works": rate_limited(retry_after=66013)}, make_transport)
    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q"})
    assert result.is_error is True
    text = result.content[0].text
    assert "midnight UTC" in text
    assert "OPENALEX_API_KEY" in text
    assert "Do not retry" in text
    assert "66013" in text


async def test_an_unrecognized_identifier_names_the_accepted_forms(
    settings, make_transport
) -> None:
    """`ToolError`, not a crash: a crash tells the model only 'Error executing tool'."""
    served = serve(settings, {}, make_transport)
    async with served.client as session:
        result = await session.call_tool("get_paper", {"paper_id": "some-title-slug"})
    assert result.is_error is True
    text = result.content[0].text
    assert "OpenAlex ID" in text
    assert "DOI" in text
    assert "Semantic Scholar ID" in text
    assert served.seen == []


async def test_a_semantic_scholar_id_without_a_key_explains_the_way_out(
    settings, make_transport
) -> None:
    served = serve(settings, {}, make_transport)
    async with served.client as session:
        result = await session.call_tool("get_paper", {"paper_id": "CorpusId:205076355"})
    assert result.is_error is True
    text = result.content[0].text
    assert "SEMANTIC_SCHOLAR_API_KEY" in text
    # It has to say what to do instead, or the agent stops dead.
    assert "DOI" in text
    assert served.seen == []


async def test_format_citation_reports_the_missing_key_too(settings, make_transport) -> None:
    """Both tools route through `_resolve`, so both must fail the same readable way."""
    served = serve(settings, {}, make_transport)
    async with served.client as session:
        result = await session.call_tool("format_citation", {"doi": "ArXiv:2010.14395"})
    assert result.is_error is True
    assert "SEMANTIC_SCHOLAR_API_KEY" in result.content[0].text


async def test_a_404_says_what_was_not_found(settings, make_transport) -> None:
    served = serve(settings, {}, make_transport)
    async with served.client as session:
        result = await session.call_tool("get_paper", {"paper_id": REAL_ID})
    assert result.is_error is True
    assert REAL_ID in result.content[0].text


async def test_a_failing_call_does_not_take_the_session_down(
    settings, search_payload, make_transport
) -> None:
    """A stdio server has one connection for its whole life; it cannot afford a bad call."""
    served = serve(settings, {"/works": search_payload}, make_transport)
    async with served.client as session:
        first = await session.call_tool("get_paper", {"paper_id": "not-an-identifier"})
        second = await session.call_tool("search_papers", {"query": "q"})
    assert first.is_error is True
    assert second.is_error is False
    assert second.structured_content["count"] == 1


# ---------------------------------------------------------------------------
# get_paper and format_citation
# ---------------------------------------------------------------------------


async def test_get_paper_resolves_the_bibliography(
    settings, make_transport, detail_payload, references_payload
) -> None:
    served = serve(
        settings,
        {f"/works/{REAL_ID}": detail_payload, "/works": references_payload},
        make_transport,
    )
    async with served.client as session:
        result = await session.call_tool("get_paper", {"paper_id": REAL_ID})
    assert result.is_error is False
    content = result.structured_content
    assert content["title"] == "Contrastive Learning for Sequential Recommendation"
    assert content["referenced_works_count"] == 73

    # One request per batch, not one per reference: 20 IDs, pipe-separated.
    assert len(served.seen) == 2
    assert "|" in served.seen[1].url.params["filter"]

    # Count and list disagree, which is the documented truncation signal.
    assert len(content["references"]) == settings.max_references < content["referenced_works_count"]

    wanted = [
        item.removeprefix("https://openalex.org/")
        for item in detail_payload["referenced_works"][: settings.max_references]
    ]
    assert [ref["openalex_id"] for ref in content["references"]] == wanted


async def test_an_unresolved_reference_is_listed_rather_than_dropped(
    settings, make_transport, detail_payload, references_payload
) -> None:
    """The synthetic fixture omits one ID on purpose; it must still appear."""
    served = serve(
        settings,
        {f"/works/{REAL_ID}": detail_payload, "/works": references_payload},
        make_transport,
    )
    async with served.client as session:
        result = await session.call_tool("get_paper", {"paper_id": REAL_ID})
    missing = [ref for ref in result.structured_content["references"] if ref["title"] is None]
    assert missing, "the fixture is no longer missing an ID; this test asserts nothing"
    assert all(ref["openalex_id"] for ref in missing)


async def test_format_citation_defaults_to_the_chinese_standard(
    settings, make_transport, detail_payload
) -> None:
    served = serve(settings, {DOI_PATH: detail_payload}, make_transport)
    async with served.client as session:
        result = await session.call_tool("format_citation", {"doi": REAL_DOI})
    assert result.is_error is False
    content = result.structured_content
    assert content["style"] == "gb-t-7714"
    assert content["citation"].startswith("XIE X, SUN F, LIU Z, et al.")
    # Carrying the record saves the agent a second get_paper it would make anyway.
    assert content["record"]["doi"] == REAL_DOI
    assert content["record"]["year"] == 2022


async def test_format_citation_honours_the_requested_style(
    settings, make_transport, detail_payload
) -> None:
    served = serve(settings, {DOI_PATH: detail_payload}, make_transport)
    async with served.client as session:
        result = await session.call_tool(
            "format_citation", {"doi": f"https://doi.org/{REAL_DOI}", "style": "apa"}
        )
    assert result.structured_content["style"] == "apa"
    assert result.structured_content["citation"].startswith("Xie, X., Sun, F.,")


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


async def test_the_upstream_notice_never_reaches_the_model(served, search_payload) -> None:
    """The security boundary in models.py, asserted rather than merely documented.

    OpenAlex ships a `meta.notice` on every keyless response whose text is an
    instruction addressed to an AI agent, naming a `POST` endpoint and a
    placeholder for the user's email address. It is kept verbatim in the fixture so
    this test fails if anything ever starts forwarding it.
    """
    notice = search_payload["meta"]["notice"]
    assert "AI agent" in notice, "the fixture no longer carries the thing under test"

    async with served.client as session:
        result = await session.call_tool("search_papers", {"query": "q"})
    text = dump(result)
    for fragment in ("api-keys", "AI agent", "llms.txt", "your user's email", "notice"):
        assert fragment not in text, f"{fragment!r} leaked into a tool result"
