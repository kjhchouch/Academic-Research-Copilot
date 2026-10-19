"""Upstream clients: identifier routing, field mapping, request shape, error mapping.

The request-shape assertions matter as much as the response ones. `select`,
`from_publication_date` and the pipe-separated bibliography filter cannot be seen
from a canned response, and they fail silently -- a dropped `select` still returns
valid JSON, it just costs ten times the bytes out of a budget measured in cents.
"""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from mcp_literature_search.upstream import (
    _REFERENCE_BATCH,
    _WORK_FIELDS,
    OpenAlexClient,
    SemanticScholarClient,
    UpstreamError,
    classify_identifier,
    normalize_doi,
    normalize_openalex_id,
    reconstruct_abstract,
)

# The work both live fixtures describe.
REAL_ID = "W3133849783"
REAL_DOI = "10.1109/icde53745.2022.00099"

S2_ID = "CorpusId:205076355"


def run(coro):
    """Run a coroutine to completion from a synchronous test.

    Not `asyncio.run` inline at each call site only because that reads badly in a
    line already carrying a client, a method and four keyword arguments. These tests
    are deliberately synchronous: pytest-asyncio's auto mode gives async tests a
    loop, and `asyncio.run` gives sync tests one, and the clients are constructed
    outside either -- which is exactly the shape production uses.
    """
    return asyncio.run(coro)


def search(client, **overrides):
    arguments = {"year_from": None, "limit": 5, "sort": "relevance", **overrides}
    return run(client.search(arguments.pop("query", "q"), **arguments))


# --------------------------------------------------------------------------
# identifier routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        (REAL_ID, "openalex"),
        (f"https://openalex.org/{REAL_ID}", "openalex"),
        (f"HTTPS://OPENALEX.ORG/{REAL_ID}", "openalex"),
        (REAL_DOI, "openalex"),
        (f"https://doi.org/{REAL_DOI}", "openalex"),
        ("10.1038/nature12373", "openalex"),
        ("CorpusId:215416146", "semanticscholar"),
        ("corpusid:215416146", "semanticscholar"),
        ("ArXiv:2010.14395", "semanticscholar"),
        ("DOI:10.1038/nature12373", "semanticscholar"),
        ("PubMed:26878367", "semanticscholar"),
        ("MAG:123", "semanticscholar"),
        ("ACL:W12-3903", "semanticscholar"),
        ("https://www.semanticscholar.org/paper/abc", "semanticscholar"),
        ("642f9d3d5d4b6f5f1a4d3c2b1a09876543210fed", "semanticscholar"),
    ],
)
def test_classify_identifier(identifier: str, expected: str) -> None:
    assert classify_identifier(identifier) == expected


def test_bare_doi_goes_to_openalex_but_prefixed_doi_goes_to_semantic_scholar() -> None:
    """The asymmetry is deliberate: `DOI:` is Semantic Scholar's lookup syntax."""
    assert classify_identifier("10.1038/nature12373") == "openalex"
    assert classify_identifier("DOI:10.1038/nature12373") == "semanticscholar"


@pytest.mark.parametrize(
    "identifier", ["the contrastive learning paper", "W", "12345", "arxiv 2010"]
)
def test_classify_identifier_rejects_unrecognized_forms(identifier: str) -> None:
    with pytest.raises(UpstreamError) as excinfo:
        classify_identifier(identifier)
    message = str(excinfo.value)
    # The message has to teach the caller the accepted forms, because it is read by a
    # model that will otherwise just guess again.
    assert identifier in message
    assert "OpenAlex ID" in message
    assert "DOI" in message
    assert "CorpusId" in message


@pytest.mark.parametrize("identifier", ["", "   "])
def test_classify_identifier_rejects_blank(identifier: str) -> None:
    with pytest.raises(UpstreamError):
        classify_identifier(identifier)


def test_classify_identifier_strips_surrounding_whitespace() -> None:
    assert classify_identifier(f"  {REAL_ID}\n") == "openalex"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"https://doi.org/{REAL_DOI}", REAL_DOI),
        (f"HTTPS://DOI.ORG/{REAL_DOI}", REAL_DOI),
        (f"doi.org/{REAL_DOI}", REAL_DOI),
        (f"doi:{REAL_DOI}", REAL_DOI),
        (REAL_DOI, REAL_DOI),
        ("", None),
        (None, None),
        ("https://doi.org/", None),
    ],
)
def test_normalize_doi(value: str | None, expected: str | None) -> None:
    assert normalize_doi(value) == expected


def test_normalize_doi_is_idempotent() -> None:
    """An agent echoes `paper.doi` back into `format_citation`; it must not grow a prefix."""
    once = normalize_doi(f"https://doi.org/{REAL_DOI}")
    assert once == REAL_DOI
    assert normalize_doi(once) == once


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"https://openalex.org/{REAL_ID}", REAL_ID),
        (REAL_ID, REAL_ID),
        ("", None),
        (None, None),
    ],
)
def test_normalize_openalex_id(value: str | None, expected: str | None) -> None:
    assert normalize_openalex_id(value) == expected


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        (REAL_ID, REAL_ID),
        (f"https://openalex.org/{REAL_ID}", REAL_ID),
        (REAL_DOI, f"doi:{REAL_DOI}"),
        (f"https://doi.org/{REAL_DOI}", f"doi:{REAL_DOI}"),
    ],
)
def test_work_lookup_id(identifier: str, expected: str) -> None:
    """OpenAlex's own lookup syntax, so a DOI needs no search round-trip first."""
    assert OpenAlexClient.work_lookup_id(identifier) == expected


def test_work_lookup_id_rejects_something_that_is_neither() -> None:
    with pytest.raises(UpstreamError, match="neither an OpenAlex work ID"):
        OpenAlexClient.work_lookup_id(S2_ID)


# --------------------------------------------------------------------------
# abstract reconstruction
# --------------------------------------------------------------------------


def test_reconstruct_abstract_reorders_positions() -> None:
    index = {"world": [1], "Hello": [0], "again": [2, 4], "!": [3]}
    assert reconstruct_abstract(index) == "Hello world again ! again"


def test_reconstruct_abstract_handles_empty_and_missing() -> None:
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None
    assert reconstruct_abstract({"word": []}) is None


def test_reconstruct_abstract_refuses_an_absurd_length() -> None:
    """Untrusted input whose failure mode is memory, not an exception."""
    assert reconstruct_abstract({"a": [0], "b": [50_000_000]}) is None


def test_reconstruct_abstract_ignores_negative_positions() -> None:
    assert reconstruct_abstract({"bad": [-5], "good": [0]}) == "good"


def test_reconstruct_abstract_works_on_the_real_captured_index(search_payload) -> None:
    abstract = reconstruct_abstract(search_payload["results"][0]["abstract_inverted_index"])
    assert abstract is not None
    words = abstract.split()
    # 170 positions in the captured response; the reconstruction must account for all
    # of them and must start with the words the index puts at 0..2.
    assert len(words) == 170
    assert words[:3] == ["Sequential", "recommendation", "methods"]
    assert abstract.startswith("Sequential recommendation methods play")


# --------------------------------------------------------------------------
# OpenAlex search
# --------------------------------------------------------------------------


def test_search_maps_the_real_payload(search_payload, settings, make_transport) -> None:
    transport, _ = make_transport({"/works": search_payload})
    papers, total = search(
        OpenAlexClient(settings, transport=transport), query="contrastive learning"
    )

    assert total == search_payload["meta"]["count"] == 4217397
    assert len(papers) == 1
    paper = papers[0]
    assert paper.source == "openalex"
    assert paper.title == "Contrastive Learning for Sequential Recommendation"
    assert paper.openalex_id == REAL_ID
    assert paper.year == 2022
    assert paper.type == "conference-paper"
    assert paper.language == "en"
    assert paper.cited_by_count == 726
    assert paper.is_retracted is False
    assert paper.open_access_url == "https://arxiv.org/pdf/2010.14395"
    assert paper.venue == "Proceedings - International Conference on Data Engineering"
    assert paper.first_page == "1259"
    assert paper.last_page == "1273"
    assert paper.volume is None
    assert paper.abstract is not None
    assert paper.abstract.startswith("Sequential recommendation")
    assert len(paper.authors) == 8
    assert paper.authors[0] == "Xu Xie"
    assert paper.authors[-1] == "Bin Cui"


def test_search_stores_the_doi_and_id_bare(search_payload, settings, make_transport) -> None:
    """Both arrive as URLs; both are stored bare, once, at the mapping layer."""
    transport, _ = make_transport({"/works": search_payload})
    papers, _ = search(OpenAlexClient(settings, transport=transport))
    assert papers[0].doi == REAL_DOI
    assert "doi.org" not in (papers[0].doi or "")
    assert papers[0].openalex_id == REAL_ID
    assert "openalex.org" not in (papers[0].openalex_id or "")


def test_search_request_carries_the_select_list(settings, search_payload, make_transport) -> None:
    transport, seen = make_transport({"/works": search_payload})
    search(OpenAlexClient(settings, transport=transport))
    params = seen[0].url.params
    assert params["select"] == _WORK_FIELDS
    assert params["search"] == "q"
    assert params["per-page"] == "5"


def test_search_omits_the_filter_when_year_from_is_absent(
    settings, search_payload, make_transport
) -> None:
    transport, seen = make_transport({"/works": search_payload})
    search(OpenAlexClient(settings, transport=transport))
    assert "filter" not in seen[0].url.params


def test_search_filters_by_publication_date(settings, search_payload, make_transport) -> None:
    transport, seen = make_transport({"/works": search_payload})
    search(OpenAlexClient(settings, transport=transport), year_from=2021)
    assert seen[0].url.params["filter"] == "from_publication_date:2021-01-01"


def test_search_only_states_a_sort_when_it_is_not_the_default(
    settings, search_payload, make_transport
) -> None:
    """Relevance is what OpenAlex already does when `search` is present."""
    transport, seen = make_transport({"/works": search_payload})
    client = OpenAlexClient(settings, transport=transport)
    search(client, sort="relevance")
    search(client, sort="cited")
    assert "sort" not in seen[0].url.params
    assert seen[1].url.params["sort"] == "cited_by_count:desc"


def test_search_sends_credentials_when_configured(search_payload, make_transport) -> None:
    from mcp_literature_search.config import Settings

    settings = Settings.from_env(
        {
            "OPENALEX_BASE_URL": "https://openalex.test",
            "OPENALEX_API_KEY": "k",
            "OPENALEX_MAILTO": "m@e.org",
        }
    )
    transport, seen = make_transport({"/works": search_payload})
    search(OpenAlexClient(settings, transport=transport))
    params = seen[0].url.params
    assert params["api_key"] == "k"
    assert params["mailto"] == "m@e.org"
    # OpenAlex's polite pool matches on the User-Agent, separately from mailto.
    assert "mailto:m@e.org" in seen[0].headers["user-agent"]


def test_search_drops_a_row_with_no_title(settings, make_transport) -> None:
    """A titleless row cannot be cited, shown, or rendered -- but it must not fail the page."""
    payload = {
        "meta": {"count": 2},
        "results": [
            {"id": "https://openalex.org/W1", "title": ""},
            {"id": "https://openalex.org/W2", "title": "Kept"},
        ],
    }
    transport, _ = make_transport({"/works": payload})
    papers, total = search(OpenAlexClient(settings, transport=transport))
    assert [paper.title for paper in papers] == ["Kept"]
    # total_available is upstream's number, not ours, so the disagreement is the
    # signal that a row was dropped rather than that the query matched less.
    assert total == 2


def test_search_falls_back_to_the_returned_count_when_meta_has_none(
    settings, make_transport
) -> None:
    payload = {"results": [{"id": "https://openalex.org/W1", "title": "Only"}]}
    transport, _ = make_transport({"/works": payload})
    papers, total = search(OpenAlexClient(settings, transport=transport))
    assert total == len(papers) == 1


def test_search_never_forwards_the_meta_notice(search_payload, settings, make_transport) -> None:
    """`meta.notice` is prose addressed to an AI agent. Only `count` is read.

    The real payload carries an instruction to POST the user's email address to
    OpenAlex to be issued a key. Legitimate vendor design or not, it is an
    instruction arriving on a data channel, and this is the test that keeps it there.
    """
    notice = search_payload["meta"]["notice"]
    assert "api-keys" in notice and "email" in notice  # the fixture still carries it
    transport, _ = make_transport({"/works": search_payload})
    papers, total = search(OpenAlexClient(settings, transport=transport))
    dumped = str([paper.model_dump() for paper in papers]) + str(total)
    assert "api-keys" not in dumped
    assert "email" not in dumped
    assert notice[:40] not in dumped


def test_search_raises_when_the_response_has_no_results_list(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": {"meta": {"count": 0}}})
    with pytest.raises(UpstreamError, match="without a results list"):
        search(OpenAlexClient(settings, transport=transport))


# --------------------------------------------------------------------------
# OpenAlex detail + bibliography
# --------------------------------------------------------------------------


def test_get_work_maps_the_real_detail_payload(detail_payload, settings, make_transport) -> None:
    transport, seen = make_transport({f"/works/{REAL_ID}": detail_payload})
    detail = run(OpenAlexClient(settings, transport=transport).get_work(REAL_ID))

    assert detail.source == "openalex"
    assert detail.openalex_id == REAL_ID
    assert detail.doi == REAL_DOI
    assert detail.title == "Contrastive Learning for Sequential Recommendation"
    assert detail.referenced_works_count == 73
    # with_references defaults to False: no second request, no bibliography.
    assert detail.references == []
    assert len(seen) == 1
    assert seen[0].url.params["select"].endswith(",referenced_works")


def test_get_work_accepts_a_doi(detail_payload, settings, make_transport) -> None:
    transport, seen = make_transport({f"/works/doi:{REAL_DOI}": detail_payload})
    detail = run(OpenAlexClient(settings, transport=transport).get_work(REAL_DOI))
    assert detail.openalex_id == REAL_ID
    assert seen[0].url.path == f"/works/doi:{REAL_DOI}"


def test_get_work_resolves_the_bibliography_in_one_batched_request(
    detail_payload, references_payload, settings, make_transport
) -> None:
    transport, seen = make_transport(
        {f"/works/{REAL_ID}": detail_payload, "/works": references_payload}
    )
    detail = run(
        OpenAlexClient(settings, transport=transport).get_work(REAL_ID, with_references=True)
    )

    assert len(seen) == 2
    batch = seen[1].url.params
    identifiers = batch["filter"].removeprefix("openalex_id:").split("|")
    # 73 references, capped at max_references=20, resolved in one request -- not twenty.
    assert len(identifiers) == settings.max_references == 20
    assert identifiers[0] == "W343636949"
    assert batch["per-page"] == "20"
    assert detail.referenced_works_count == 73
    assert len(detail.references) == 20


def test_resolved_references_keep_the_requested_order(
    detail_payload, references_payload, settings, make_transport
) -> None:
    """The batch endpoint answers in its own order; a bibliography must not shuffle.

    The fixture also omits one requested ID, which has to come back as an entry
    carrying nothing but its ID -- an unresolved reference beats a vanished one.
    """
    transport, _ = make_transport(
        {f"/works/{REAL_ID}": detail_payload, "/works": references_payload}
    )
    detail = run(
        OpenAlexClient(settings, transport=transport).get_work(REAL_ID, with_references=True)
    )

    assert detail.references[0].openalex_id == "W343636949"
    assert detail.references[0].title == "[fixture] Placeholder Title A"
    assert detail.references[0].year == 2021
    assert detail.references[0].doi == "10.1000/fixture.c"

    assert detail.references[1].openalex_id == "W1522301498"
    assert detail.references[1].title is None
    assert detail.references[1].doi is None

    assert detail.references[2].openalex_id == "W1682403713"
    assert detail.references[2].title == "[fixture] Placeholder Title C"
    assert detail.references[2].doi is None  # the fixture row has an explicit null


def test_bibliography_is_chunked_when_it_exceeds_one_batch(settings) -> None:
    ids = [f"W{n}" for n in range(_REFERENCE_BATCH * 2 + 3)]
    client = OpenAlexClient(
        settings,
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"results": []})),
    )
    references = run(client.resolve_references(ids))
    # Ordering has to survive being split across three requests.
    assert [ref.openalex_id for ref in references] == ids


def test_resolve_references_on_an_empty_list_makes_no_request(settings, make_transport) -> None:
    transport, seen = make_transport({})
    assert run(OpenAlexClient(settings, transport=transport).resolve_references([])) == []
    assert seen == []


def test_get_work_without_referenced_works_reports_zero(settings, make_transport) -> None:
    payload = {"id": f"https://openalex.org/{REAL_ID}", "title": "T"}
    transport, _ = make_transport({f"/works/{REAL_ID}": payload})
    detail = run(OpenAlexClient(settings, transport=transport).get_work(REAL_ID))
    assert detail.referenced_works_count == 0
    assert detail.references == []


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


def test_not_found_names_the_identifier_rather_than_the_url(settings, make_transport) -> None:
    transport, _ = make_transport({})  # every unrouted path answers 404
    with pytest.raises(UpstreamError) as excinfo:
        run(OpenAlexClient(settings, transport=transport).get_work(REAL_ID))
    assert excinfo.value.status == 404
    assert REAL_ID in str(excinfo.value)


def test_rate_limit_is_actionable_and_carries_the_wait(
    settings, make_transport, rate_limited
) -> None:
    """The consumer of this text is a model deciding whether to retry."""
    transport, _ = make_transport({"/works": rate_limited(retry_after=66013)})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    error = excinfo.value
    message = str(error)
    assert error.status == 429
    assert error.retry_after == 66013
    assert "66013" in message
    assert "midnight UTC" in message
    assert "OPENALEX_API_KEY" in message
    assert "Do not retry" in message


def test_rate_limit_message_differs_by_service(settings, make_transport, rate_limited) -> None:
    transport, _ = make_transport({f"/paper/{S2_ID}": rate_limited(service="Semantic Scholar")})
    with pytest.raises(UpstreamError) as excinfo:
        run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert "SEMANTIC_SCHOLAR_API_KEY" in str(excinfo.value)


def test_retry_after_is_read_from_the_header_when_there_is_no_body(
    settings, make_transport
) -> None:
    transport, _ = make_transport(
        {"/works": httpx2.Response(429, headers={"retry-after": "37"}, text="")}
    )
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert excinfo.value.retry_after == 37


def test_server_errors_say_the_upstream_is_at_fault(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": httpx2.Response(503, text="upstream down")})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert excinfo.value.status == 503
    assert "not a problem with the query" in str(excinfo.value)


def test_auth_errors_point_at_the_key(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": httpx2.Response(403, text="forbidden")})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert excinfo.value.status == 403
    assert "credentials" in str(excinfo.value)


def test_other_client_errors_include_truncated_upstream_text(settings, make_transport) -> None:
    body = "bad  \n\n filter syntax " + "x" * 500
    transport, _ = make_transport({"/works": httpx2.Response(400, text=body)})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    message = str(excinfo.value)
    assert excinfo.value.status == 400
    assert "bad filter syntax" in message  # whitespace flattened onto one line
    assert len(message) < 400  # truncated: this text is about to enter a prompt


def test_timeout_is_reported_as_a_timeout(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": httpx2.ReadTimeout("slow")})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert "ReadTimeout" in str(excinfo.value)
    assert excinfo.value.status is None


def test_connection_failure_is_reported_as_a_network_error(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": httpx2.ConnectError("dns failed")})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert "network error" in str(excinfo.value)


def test_a_non_json_body_does_not_escape_as_a_decode_error(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": httpx2.Response(200, text="<html>maintenance</html>")})
    with pytest.raises(UpstreamError, match="not JSON"):
        search(OpenAlexClient(settings, transport=transport))


def test_a_non_object_body_does_not_escape_as_an_attribute_error(settings, make_transport) -> None:
    transport, _ = make_transport({"/works": [1, 2, 3]})
    with pytest.raises(UpstreamError, match="not JSON|non-object|results list"):
        search(OpenAlexClient(settings, transport=transport))


def test_no_error_message_contains_the_api_key(make_transport) -> None:
    """OpenAlex's key travels as a query parameter, so it is one f-string from a log.

    `_get_json` reports the path it was handed, never the assembled URL. This is the
    test that keeps it that way: if someone "improves" a message by interpolating
    `request.url`, the credential starts landing in stderr and in every tool result
    the model reads.
    """
    from mcp_literature_search.config import Settings

    settings = Settings.from_env(
        {"OPENALEX_BASE_URL": "https://openalex.test", "OPENALEX_API_KEY": "sk-super-secret-value"}
    )
    transport, seen = make_transport({"/works": httpx2.Response(500, text="boom")})
    with pytest.raises(UpstreamError) as excinfo:
        search(OpenAlexClient(settings, transport=transport))
    assert "sk-super-secret-value" in str(seen[0].url)  # the request really did carry it
    assert "sk-super-secret-value" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Semantic Scholar
# --------------------------------------------------------------------------

S2_PAYLOAD = {
    "paperId": "642f9d3d5d4b6f5f1a4d3c2b1a09876543210fed",
    "externalIds": {"DOI": "10.1038/nature12373", "ArXiv": "1301.3781", "CorpusId": 205076355},
    "title": "Finding Structure in Time",
    "abstract": "An abstract.",
    "venue": "Fallback Venue",
    "year": 2015,
    "language": "en",
    "referenceCount": 2,
    "citationCount": 12345,
    "publicationTypes": ["JournalArticle"],
    "journal": {"name": "Nature", "volume": "521", "pages": "436-444"},
    "isOpenAccess": True,
    "openAccessPdf": {"url": "https://arxiv.org/pdf/1301.3781"},
    "authors": [{"authorId": "1", "name": "Xu Xie"}, {"authorId": "2", "name": "Fei Sun"}],
    "references": [
        {"title": "Ref one", "year": 2010, "externalIds": {"DOI": "10.1/a"}},
        {"title": "Ref two", "year": None, "externalIds": {}},
        {"title": None},
    ],
}


def test_semantic_scholar_maps_onto_the_normalized_record(settings, make_transport) -> None:
    transport, _ = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))

    assert detail.source == "semanticscholar"
    assert detail.title == "Finding Structure in Time"
    assert detail.doi == "10.1038/nature12373"
    assert detail.corpus_id == 205076355
    assert detail.openalex_id is None
    assert detail.cited_by_count == 12345
    assert detail.authors == ["Xu Xie", "Fei Sun"]
    assert detail.year == 2015
    # `venue` and `journal.name` are both present and disagree; the journal wins
    # because it is the more specific field.
    assert detail.venue == "Nature"
    assert detail.volume == "521"
    # S2 packs the page range into one string where OpenAlex splits it in two.
    assert detail.first_page == "436"
    assert detail.last_page == "444"
    assert detail.open_access_url == "https://arxiv.org/pdf/1301.3781"
    assert detail.abstract == "An abstract."
    assert detail.referenced_works_count == 2


def test_semantic_scholar_falls_back_to_venue_when_there_is_no_journal(
    settings, make_transport
) -> None:
    transport, _ = make_transport({f"/paper/{S2_ID}": {**S2_PAYLOAD, "journal": None}})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert detail.venue == "Fallback Venue"
    assert detail.volume is None


def test_semantic_scholar_type_is_folded_onto_the_openalex_vocabulary(
    settings, make_transport
) -> None:
    """One vocabulary, so citation.py maintains one marker table instead of two."""
    transport, _ = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert detail.type == "article"  # JournalArticle -> article, i.e. GB/T's [J]


def test_semantic_scholar_conference_type_maps_to_a_proceedings_marker(
    settings, make_transport
) -> None:
    payload = {**S2_PAYLOAD, "publicationTypes": ["Conference"], "journal": None}
    transport, _ = make_transport({f"/paper/{S2_ID}": payload})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert detail.type == "conference-paper"


def test_semantic_scholar_unknown_type_passes_through_lowercased(settings, make_transport) -> None:
    payload = {**S2_PAYLOAD, "publicationTypes": ["SomethingNew"]}
    transport, _ = make_transport({f"/paper/{S2_ID}": payload})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    # Falls through to GB/T's [Z] rather than being guessed into a real marker.
    assert detail.type == "somethingnew"


def test_semantic_scholar_references_are_mapped(settings, make_transport) -> None:
    transport, _ = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert [ref.title for ref in detail.references] == ["Ref one", "Ref two", None]
    assert detail.references[0].doi == "10.1/a"
    assert detail.references[1].doi is None
    assert detail.references[2].doi is None


def test_semantic_scholar_sends_the_key_as_a_header_never_a_parameter(make_transport) -> None:
    from mcp_literature_search.config import Settings

    settings = Settings.from_env(
        {
            "SEMANTIC_SCHOLAR_BASE_URL": "https://semanticscholar.test",
            "SEMANTIC_SCHOLAR_API_KEY": "s2-secret",
        }
    )
    transport, seen = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert seen[0].headers["x-api-key"] == "s2-secret"
    # A query parameter would land in upstream access logs and in every error message
    # that quotes a URL.
    assert "s2-secret" not in str(seen[0].url)
    assert "api_key" not in seen[0].url.params


def test_semantic_scholar_without_a_key_sends_no_auth_header(settings, make_transport) -> None:
    transport, seen = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert "x-api-key" not in seen[0].headers


def test_semantic_scholar_requests_the_fields_it_maps(settings, make_transport) -> None:
    """An unrequested field comes back null, which is indistinguishable from absent."""
    transport, seen = make_transport({f"/paper/{S2_ID}": S2_PAYLOAD})
    run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    fields = seen[0].url.params["fields"]
    for needed in (
        "title",
        "externalIds",
        "citationCount",
        "journal",
        "authors.name",
        "references.title",
    ):
        assert needed in fields


def test_semantic_scholar_survives_a_minimal_payload(settings, make_transport) -> None:
    """Every field but `title` absent must produce a record, not a KeyError."""
    transport, _ = make_transport({f"/paper/{S2_ID}": {"title": "Bare"}})
    detail = run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert detail.title == "Bare"
    assert detail.authors == []
    assert detail.year is None
    assert detail.venue is None
    assert detail.doi is None
    assert detail.corpus_id is None
    assert detail.type is None
    assert detail.references == []
    assert detail.referenced_works_count == 0


def test_semantic_scholar_not_found_names_the_identifier(settings, make_transport) -> None:
    transport, _ = make_transport({})
    with pytest.raises(UpstreamError) as excinfo:
        run(SemanticScholarClient(settings, transport=transport).get_paper(S2_ID))
    assert excinfo.value.status == 404
    assert S2_ID in str(excinfo.value)
