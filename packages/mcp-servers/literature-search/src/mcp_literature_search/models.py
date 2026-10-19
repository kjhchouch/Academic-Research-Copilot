"""Normalized shapes returned by the tools.

Both upstreams are mapped into these models rather than passed through. The usual
justification is that a tool contract should not change when a data source does;
the one that matters here is trust. Everything a tool returns is read by a model,
so the declared fields act as a whitelist -- anything an upstream sends that these
models do not name is dropped instead of forwarded.

That is not a hypothetical. OpenAlex currently ships a `meta.notice` string on
every keyless response, and its text is an instruction addressed to an AI agent:
it tells the agent to POST the user's email address to an endpoint in order to be
issued an API key. It may well be legitimate vendor design, but it is still an
instruction arriving on a data channel, from a service this project already
classifies as untrusted input. Nothing in this package reads `meta` except
`count`. See docs/devlog.md, D5.

Every model here is also a tool's return type, which the MCP SDK turns into an
`outputSchema` on the wire. So these fields are simultaneously the security
boundary and the published contract the agent codes against -- two reasons to keep
them narrow.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["openalex", "semanticscholar"]

#: What `search_papers` can order by. Relevance is OpenAlex's own ranking against
#: the query; `cited` sorts by citation count descending, which is what "find the
#: representative work for this method" actually needs and what a relevance sort
#: will not surface.
Sort = Literal["relevance", "cited"]

#: The three styles `format_citation` accepts. `gb-t-7714` is the Chinese national
#: standard and the default, because the product writes for Chinese journals; the
#: plan lists it last and the others are there for submissions abroad.
CitationStyle = Literal["apa", "ieee", "gb-t-7714"]


class Paper(BaseModel):
    """One work, normalized across upstreams.

    Optional fields stay optional rather than defaulting to a plausible-looking
    empty string: a citation renderer that cannot tell "no volume" from "volume
    is the empty string" will happily print `vol. ,` into someone's reference
    list.
    """

    source: Source
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    #: Bare DOI -- `10.1109/icde53745.2022.00099`, not the `https://doi.org/`
    #: URL OpenAlex returns. Normalized once here so the three citation styles do
    #: not each have to strip the prefix, and so an agent that echoes the field
    #: back into `format_citation` cannot produce a doubled prefix.
    doi: str | None = None
    openalex_id: str | None = None
    corpus_id: int | None = None
    cited_by_count: int = 0
    type: str | None = None
    language: str | None = None
    abstract: str | None = None
    volume: str | None = None
    issue: str | None = None
    first_page: str | None = None
    last_page: str | None = None
    #: Surfaced rather than filtered out. A retracted paper that reads like a
    #: normal citation is the worst possible outcome for a tool whose job is to
    #: make writing verifiable, so the flag travels with the record and Week 3's
    #: citation check can act on it.
    is_retracted: bool = False
    open_access_url: str | None = None


class Reference(BaseModel):
    """One entry of a bibliography, as resolved by `get_paper`."""

    title: str | None = None
    year: int | None = None
    doi: str | None = None
    openalex_id: str | None = None


class PaperDetail(Paper):
    """A single work plus its bibliography.

    `references` is truncated to `Settings.max_references` while
    `referenced_works_count` is always the true total, so a caller can tell "this
    paper cites nothing" from "we stopped listing them".
    """

    references: list[Reference] = Field(default_factory=list)
    referenced_works_count: int = 0


class SearchResult(BaseModel):
    """Return type of `search_papers`."""

    query: str
    sort: Sort
    count: int
    #: Total matches upstream, which is usually far larger than `count`. Present
    #: so the agent can say "1,204 papers match; here are the 5 most cited" rather
    #: than implying the list is exhaustive.
    total_available: int = 0
    #: True only when the requested `limit` exceeded the server's ceiling and was
    #: clamped. Not "there are more matches" -- that is always true, and is what
    #: `total_available` communicates.
    truncated: bool = False
    papers: list[Paper] = Field(default_factory=list)


class CitationResult(BaseModel):
    """Return type of `format_citation`.

    Carries the resolved `record` alongside the rendered string. Formatting from a
    DOI requires fetching the metadata anyway, and handing it back costs nothing
    while saving the agent a second `get_paper` call it would otherwise make to
    check the authors and year it is about to put in a bibliography.
    """

    style: CitationStyle
    citation: str
    record: Paper
