"""HTTP clients for OpenAlex and Semantic Scholar.

**No retries.** A 429 from either service is a budget message with a
`retryAfter` in it, and the keyless OpenAlex budget is shared per egress IP and
resets at midnight UTC -- retrying inside the tool cannot help, it only spends the
seconds the user is waiting. `server.py` converts `UpstreamError` into an MCP
`ToolError` whose text the model reads, so the decision to try again belongs to
the agent, which is the only party that knows whether the user is still there.
Same reasoning as `NO_RETRY` in `paper_agent/routing/jev.py`.

**Error text is written for a model, not a human.** "429 Too Many Requests" tells
an agent nothing it can act on; "shared keyless budget exhausted, resets at
midnight UTC, or set OPENALEX_API_KEY" tells it to stop and say so. This is the
one place where message quality is a functional requirement rather than polish,
because the consumer decides what to do next.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx2

from mcp_literature_search.config import Settings
from mcp_literature_search.models import Paper, PaperDetail, Reference, Sort

logger = logging.getLogger(__name__)

IdentifierKind = Literal["openalex", "semanticscholar"]

# Fields pulled for every OpenAlex work. `select` is not an optimization bolted on
# later: the unfiltered Work object has 48 keys including `counts_by_year`,
# `related_works`, `concepts` and `topics`, and this server runs on a budget
# measured in cents per day. Naming the fields we map also means a new upstream
# field cannot reach a model without a change here, which is the whitelist in
# models.py enforced at the network layer too.
_WORK_FIELDS = (
    "id,doi,title,publication_year,type,language,cited_by_count,authorships,"
    "primary_location,biblio,abstract_inverted_index,is_retracted,open_access"
)
_DETAIL_FIELDS = _WORK_FIELDS + ",referenced_works"

_REFERENCE_FIELDS = "id,doi,title,publication_year"

# OpenAlex accepts pipe-separated values in a filter, so a whole bibliography can
# be resolved in one request. The cap keeps that request's URL inside the length
# limits both the API and the HTTP stack impose; `Settings.max_references` is
# clamped to it.
_REFERENCE_BATCH = 20

# An inverted index maps word -> positions. A position of 10**9 in a malformed or
# hostile response would make us allocate a gigabyte to render an abstract, so the
# reconstruction refuses anything past a plausible paper length instead of
# trusting the input.
_MAX_ABSTRACT_WORDS = 20_000

_DOI_PREFIX = "https://doi.org/"
_OPENALEX_PREFIX = "https://openalex.org/"

# Semantic Scholar's `publicationTypes` vocabulary, folded onto OpenAlex's so
# citation.py has one set of document-type markers to maintain. Values not listed
# fall through lowercased and render as GB/T's `[Z]` (未定义类型), which is honest
# about not knowing rather than guessing a marker that implies a format we did not
# verify.
_S2_TYPE_TO_OPENALEX = {
    "journalarticle": "article",
    "conference": "conference-paper",
    "review": "review",
    "book": "book",
    "booksection": "book-chapter",
    "dataset": "dataset",
    "editorial": "editorial",
    "lettersandcomments": "letter",
    "metaanalysis": "article",
    "casereport": "article",
    "clinicaltrial": "article",
    "news": "other",
    "study": "study",
}


class UpstreamError(Exception):
    """A literature API call failed in a way the tool should report, not crash on.

    `status` and `retry_after` are kept as attributes rather than only baked into
    the message so tests can assert on the cause instead of substring-matching
    prose.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def classify_identifier(identifier: str) -> IdentifierKind:
    """Decide which upstream owns an identifier.

    `get_paper` takes one string because the agent has one string -- whatever it
    got back from `search_papers`, or a DOI the user pasted. Routing by shape
    keeps both upstreams reachable (as the Week 1 plan wants) while leaving the
    reliable one on the default path: Semantic Scholar returns 429 on the first
    unkeyed request from a shared IP, so a DOI or a `W...` ID should not end up
    there.

    The `DOI:` prefix goes to Semantic Scholar while a bare `10.` goes to OpenAlex.
    That asymmetry is intentional: the colon-prefixed forms (`CorpusId:`, `ArXiv:`,
    `DOI:`, `PubMed:`, `MAG:`, `ACL:`) are Semantic Scholar's documented lookup
    syntax, and OpenAlex would reject them.
    """
    value = identifier.strip()
    lowered = value.lower()

    if lowered.startswith(("corpusid:", "arxiv:", "pubmed:", "mag:", "acl:", "doi:")):
        return "semanticscholar"
    if "semanticscholar.org" in lowered:
        return "semanticscholar"
    # 40 hex characters is Semantic Scholar's native paperId.
    if re.fullmatch(r"[0-9a-f]{40}", lowered):
        return "semanticscholar"
    if "openalex.org" in lowered or re.fullmatch(r"W\d+", value):
        return "openalex"
    if lowered.startswith("10.") or "doi.org" in lowered:
        return "openalex"
    raise UpstreamError(
        f"unrecognized paper identifier {identifier!r}; expected an OpenAlex ID "
        "(W2741809807), a DOI (10.1038/nature12373), or a Semantic Scholar ID "
        "(CorpusId:215416146)"
    )


def normalize_doi(value: str | None) -> str | None:
    """`https://doi.org/10.1038/x` -> `10.1038/x`. Idempotent."""
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith(_DOI_PREFIX):
        text = text[len(_DOI_PREFIX) :]
    elif text.lower().startswith("doi.org/"):
        text = text[len("doi.org/") :]
    elif text.lower().startswith("doi:"):
        text = text[len("doi:") :]
    return text.rstrip("/") or None


def normalize_openalex_id(value: str | None) -> str | None:
    """`https://openalex.org/W2741809807` -> `W2741809807`. Idempotent."""
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith(_OPENALEX_PREFIX):
        text = text[len(_OPENALEX_PREFIX) :]
    return text or None


def reconstruct_abstract(index: Mapping[str, Sequence[int]] | None) -> str | None:
    """Rebuild prose from OpenAlex's inverted-index abstract representation.

    OpenAlex does not return an abstract string; it returns `{word: [positions]}`
    as a storage optimization. Reconstructing it is not optional -- the plan lists
    abstract as part of what `search_papers` returns, and an agent cannot judge
    relevance from a title alone.
    """
    if not index:
        return None
    highest = -1
    for positions in index.values():
        for position in positions:
            if position > highest:
                highest = position
    if highest < 0:
        return None
    if highest >= _MAX_ABSTRACT_WORDS:
        # Untrusted input, and the failure mode is memory rather than an exception.
        logger.warning("abstract inverted index claims %d words; not reconstructing", highest + 1)
        return None
    slots: list[str | None] = [None] * (highest + 1)
    for word, positions in index.items():
        for position in positions:
            # Later words win on a duplicate position. A well-formed index has no
            # duplicates, and picking deterministically beats raising on data we
            # cannot fix.
            if 0 <= position <= highest:
                slots[position] = word
    return " ".join(word for word in slots if word is not None) or None


async def _get_json(
    client: httpx2.AsyncClient,
    url: str,
    *,
    source: IdentifierKind,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    not_found: str | None = None,
) -> Any:
    """GET and decode JSON, mapping every failure onto `UpstreamError`.

    `not_found` is the human-readable subject for a 404 message. Without it the
    best available text is "404 for url https://api.openalex.org/works/W999",
    which sends the agent off to guess what W999 was.

    `source` names the service for the messages that differ by service. It is
    passed rather than derived from `client.base_url`, because the one message
    where it matters most (a 429) must stay correct when a test points the base
    URL somewhere else entirely.
    """
    # The path only. `params` can carry OpenAlex's `api_key`, and httpx2's own INFO
    # log line prints the full URL -- which is why `main()` turns that logger down
    # and this is the request log instead.
    logger.info("GET %s %s", source, url)
    try:
        response = await client.get(url, params=params, headers=headers)
    except httpx2.TimeoutException as exc:
        raise UpstreamError(
            f"{type(exc).__name__} after {client.timeout.read}s contacting {url}; "
            "the upstream is slow or unreachable"
        ) from exc
    except httpx2.HTTPError as exc:
        # Transport-level: DNS, TLS, connection reset. Distinct from a status code
        # because retrying these can succeed while retrying a 429 cannot.
        raise UpstreamError(f"network error contacting {url}: {type(exc).__name__}") from exc

    if response.status_code == 404:
        raise UpstreamError(f"not found: {not_found or url}", status=404)
    if response.status_code == 429:
        raise UpstreamError(
            _rate_limit_message(response, url, source),
            status=429,
            retry_after=_retry_after(response),
        )
    if response.status_code in (401, 403):
        raise UpstreamError(
            f"{url} rejected the credentials ({response.status_code}); check the API key "
            "environment variable for this service",
            status=response.status_code,
        )
    if response.status_code >= 500:
        raise UpstreamError(
            f"{url} returned {response.status_code}; the upstream is failing, this is "
            "not a problem with the query",
            status=response.status_code,
        )
    if response.status_code >= 400:
        raise UpstreamError(
            f"{url} returned {response.status_code}: {_short_text(response)}",
            status=response.status_code,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamError(f"{url} returned a body that is not JSON") from exc


def _short_text(response: httpx2.Response, limit: int = 200) -> str:
    """Upstream error text, truncated and flattened to one line.

    Truncated because it is about to be put into a prompt, and flattened because
    `%r`-style multiline text in a tool result reads as several messages.
    """
    text = " ".join(response.text.split())
    return text[:limit]


def _retry_after(response: httpx2.Response) -> int | None:
    """Seconds until the budget resets, from the header or the body.

    OpenAlex puts `retryAfter` in the JSON body and not in the standard header, so
    both are checked. Returned as data because "retry in 68152s" and "retry in 2s"
    call for completely different advice to the user.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return int(header)
        except ValueError:
            pass
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    for key in ("retryAfter", "retry_after"):
        value = body.get(key)
        if isinstance(value, int | float):
            return int(value)
    return None


#: 429 advice per service, as (why it happened, what to do about it). The two are
#: genuinely different situations and telling them apart is the whole point: OpenAlex
#: without a key shares a per-IP daily budget that resets at midnight UTC, while
#: Semantic Scholar without a key rejects essentially immediately. A model that gets
#: the first diagnosis when the second is true will wait for a reset that is not
#: coming instead of switching identifiers.
_RATE_LIMIT_ADVICE: dict[IdentifierKind, tuple[str, str]] = {
    "openalex": (
        "OpenAlex's keyless budget is shared per egress IP and resets at midnight UTC",
        "set OPENALEX_API_KEY for a private budget",
    ),
    "semanticscholar": (
        "Semantic Scholar rate limits unkeyed requests very tightly",
        "set SEMANTIC_SCHOLAR_API_KEY",
    ),
}


def _rate_limit_message(response: httpx2.Response, url: str, source: IdentifierKind) -> str:
    """Build the 429 text, naming the fix rather than just the symptom.

    The service is a parameter, not something inferred from `url`: callers pass a
    path (`/works`, `/paper/CorpusId:1`) and the base URL lives on the client, so
    the host is never in this string. Sniffing for "openalex" here silently sent
    every OpenAlex budget error down the Semantic Scholar branch, which advises the
    wrong environment variable and the wrong reset rule.
    """
    detail, fix = _RATE_LIMIT_ADVICE[source]
    retry_after = _retry_after(response)
    wait = f"; retry after {retry_after}s" if retry_after else ""
    return f"rate limited by {source} {url}: {detail}{wait}. Do not retry now -- {fix}."


def _make_client(
    settings: Settings, *, source: IdentifierKind, base_url: str, transport: Any | None
) -> httpx2.AsyncClient:
    headers = {"Accept": "application/json"}
    if source == "openalex" and settings.openalex_mailto:
        # OpenAlex's polite pool is keyed on a reachable contact address. Sent as a
        # parameter below as well; the header is what its logs match on. Matched on
        # `source` rather than on the host text, so it still applies when a test or
        # a proxy points the base URL elsewhere.
        headers["User-Agent"] = f"paper-agent/0.1 (mailto:{settings.openalex_mailto})"
    return httpx2.AsyncClient(
        base_url=base_url,
        timeout=httpx2.Timeout(settings.timeout_seconds),
        headers=headers,
        transport=transport,
        follow_redirects=True,
    )


class OpenAlexClient:
    """OpenAlex Works API.

    `transport` is the test seam -- pass an `httpx2.MockTransport` to exercise the
    whole mapping path with no network and no budget. Prefer it over patching
    methods, for the same reason `JevBackend` takes a transport.
    """

    source: IdentifierKind = "openalex"

    def __init__(self, settings: Settings, *, transport: Any | None = None) -> None:
        self._settings = settings
        self._client = _make_client(
            settings,
            source=self.source,
            base_url=settings.openalex_base_url,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        key = self._settings.openalex_api_key
        if key is not None:
            # A query parameter, not a header: that is what OpenAlex documents. It
            # is the one credential in this package that travels in a URL, so it is
            # deliberately excluded from every error message -- `_get_json` reports
            # the path it was given, and callers pass paths without params.
            params["api_key"] = key.get_secret_value()
        if self._settings.openalex_mailto:
            params["mailto"] = self._settings.openalex_mailto
        return params

    async def search(
        self, query: str, *, year_from: int | None, limit: int, sort: Sort
    ) -> tuple[list[Paper], int]:
        """Search works. Returns the page and the upstream's total match count."""
        filters: list[str] = []
        if year_from is not None:
            # `from_publication_date` rather than a `publication_year` range: it is
            # the filter OpenAlex indexes for this, and it degrades gracefully for
            # works whose year is known but whose exact date is not.
            filters.append(f"from_publication_date:{year_from}-01-01")
        params: dict[str, Any] = {
            **self._auth_params(),
            "search": query,
            "per-page": limit,
            "select": _WORK_FIELDS,
        }
        if filters:
            params["filter"] = ",".join(filters)
        if sort == "cited":
            # Relevance is OpenAlex's default when `search` is present, so only the
            # other order needs to be stated.
            params["sort"] = "cited_by_count:desc"

        payload = await self._get("/works", params, not_found=f"no works matching {query!r}")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise UpstreamError("OpenAlex returned a response without a results list")
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total = meta.get("count")
        # A row with no title cannot be cited, cannot be shown to the user, and
        # would render as an empty string in all three styles. Dropping it beats
        # failing the whole search over one malformed record.
        papers = [
            paper_from_openalex(item)
            for item in results
            if isinstance(item, dict) and (item.get("title") or item.get("display_name"))
        ]
        return papers, int(total) if isinstance(total, int) else len(papers)

    async def get_work(self, identifier: str, *, with_references: bool = False) -> PaperDetail:
        """Fetch one work, optionally with its bibliography resolved to titles."""
        work_id = self.work_lookup_id(identifier)
        payload = await self._get(
            f"/works/{work_id}",
            {**self._auth_params(), "select": _DETAIL_FIELDS},
            not_found=f"OpenAlex work {identifier!r}",
        )
        if not isinstance(payload, dict):
            raise UpstreamError(f"OpenAlex returned a non-object for {identifier!r}")
        detail = _detail_from_openalex(payload)

        raw_refs = payload.get("referenced_works")
        if with_references and isinstance(raw_refs, list) and raw_refs:
            wanted = [normalize_openalex_id(value) for value in raw_refs if isinstance(value, str)][
                : self._settings.max_references
            ]
            wanted = [value for value in wanted if value]
            detail.references = await self.resolve_references(wanted)
        detail.referenced_works_count = (
            len(raw_refs) if isinstance(raw_refs, list) else detail.referenced_works_count
        )
        return detail

    @staticmethod
    def work_lookup_id(identifier: str) -> str:
        """Turn a DOI or OpenAlex ID into the path segment OpenAlex expects.

        `W123` is used directly; a DOI becomes `doi:10.1038/x`, which is OpenAlex's
        documented form and the reason a DOI needs no search round-trip first.
        """
        text = identifier.strip()
        if text.lower().startswith(_OPENALEX_PREFIX):
            text = text[len(_OPENALEX_PREFIX) :]
        if re.fullmatch(r"W\d+", text):
            return text
        doi = normalize_doi(text)
        if doi and doi.lower().startswith("10."):
            return f"doi:{doi}"
        raise UpstreamError(
            f"{identifier!r} is neither an OpenAlex work ID (W followed by digits) nor a DOI"
        )

    async def resolve_references(self, openalex_ids: Sequence[str]) -> list[Reference]:
        """Resolve bibliography IDs to titles in as few requests as possible.

        OpenAlex returns `referenced_works` as bare IDs. Fetching each one would
        cost a request per reference out of a budget of roughly a hundred a day, so
        the IDs are batched into one `filter=openalex_id:A|B|C` query per
        `_REFERENCE_BATCH`.
        """
        if not openalex_ids:
            return []
        resolved: dict[str, Reference] = {}
        for start in range(0, len(openalex_ids), _REFERENCE_BATCH):
            chunk = list(openalex_ids[start : start + _REFERENCE_BATCH])
            payload = await self._get(
                "/works",
                {
                    **self._auth_params(),
                    "filter": f"openalex_id:{'|'.join(chunk)}",
                    "select": _REFERENCE_FIELDS,
                    "per-page": len(chunk),
                },
                not_found="the referenced works",
            )
            results = payload.get("results") if isinstance(payload, dict) else None
            for item in results if isinstance(results, list) else []:
                if not isinstance(item, dict):
                    continue
                work_id = normalize_openalex_id(item.get("id"))
                if work_id is None:
                    continue
                resolved[work_id] = Reference(
                    title=item.get("title") or item.get("display_name"),
                    year=item.get("publication_year"),
                    doi=normalize_doi(item.get("doi")),
                    openalex_id=work_id,
                )
        # Re-keyed into the requested order. The batched endpoint returns rows in
        # its own order, and a bibliography that silently reorders itself is the
        # kind of wrong nobody notices until a reviewer does. IDs the batch did not
        # come back with still appear, with their ID and nothing else -- an
        # unresolved reference is more useful than a dropped one.
        return [resolved.get(work_id) or Reference(openalex_id=work_id) for work_id in openalex_ids]

    async def _get(self, path: str, params: Mapping[str, Any], *, not_found: str) -> Any:
        return await _get_json(
            self._client, path, source=self.source, params=params, not_found=not_found
        )


class SemanticScholarClient:
    """Semantic Scholar Graph API, for `CorpusId:` / `ArXiv:` / paperId lookups.

    Requires `SEMANTIC_SCHOLAR_API_KEY`: the unkeyed endpoint returns 429 on the
    first request from a shared egress IP, so "no key" is treated as "no backend"
    and `server.py` reports that instead of issuing a request that will fail.
    """

    source: IdentifierKind = "semanticscholar"

    def __init__(self, settings: Settings, *, transport: Any | None = None) -> None:
        self._settings = settings
        self._client = _make_client(
            settings,
            source=self.source,
            base_url=settings.semanticscholar_base_url,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        key = self._settings.semanticscholar_api_key
        # Header, not query parameter: unlike OpenAlex this credential never
        # appears in a URL, so it cannot land in an upstream access log or in an
        # error message that quotes the request path.
        return {"x-api-key": key.get_secret_value()} if key is not None else {}

    async def get_paper(self, identifier: str) -> PaperDetail:
        fields = (
            "title,abstract,year,venue,language,externalIds,citationCount,referenceCount,"
            "publicationTypes,journal,isOpenAccess,openAccessPdf,authors.name,"
            "references.title,references.year,references.externalIds"
        )
        payload = await _get_json(
            self._client,
            f"/paper/{identifier.strip()}",
            source=self.source,
            params={"fields": fields},
            headers=self._headers(),
            not_found=f"Semantic Scholar paper {identifier!r}",
        )
        if not isinstance(payload, dict):
            raise UpstreamError(f"Semantic Scholar returned a non-object for {identifier!r}")
        return _detail_from_semanticscholar(payload)


def _author_names(authorships: Any) -> list[str]:
    if not isinstance(authorships, list):
        return []
    names: list[str] = []
    for entry in authorships:
        if not isinstance(entry, dict):
            continue
        author = entry.get("author")
        display = author.get("display_name") if isinstance(author, dict) else None
        if not display:
            display = entry.get("raw_author_name")
        if isinstance(display, str) and display.strip():
            names.append(display.strip())
    return names


def _venue(work: Mapping[str, Any]) -> str | None:
    location = work.get("primary_location")
    if isinstance(location, dict):
        source = location.get("source")
        if isinstance(source, dict) and isinstance(source.get("display_name"), str):
            return source["display_name"]
    return None


def _biblio(work: Mapping[str, Any]) -> dict[str, str | None]:
    biblio = work.get("biblio")
    if not isinstance(biblio, dict):
        return {"volume": None, "issue": None, "first_page": None, "last_page": None}
    return {
        key: (str(biblio[key]) if biblio.get(key) not in (None, "") else None)
        for key in ("volume", "issue", "first_page", "last_page")
    }


def _paper_kwargs_from_openalex(work: Mapping[str, Any]) -> dict[str, Any]:
    open_access = work.get("open_access")
    oa_url = open_access.get("oa_url") if isinstance(open_access, dict) else None
    return {
        "source": "openalex",
        "title": work.get("title") or work.get("display_name") or "",
        "authors": _author_names(work.get("authorships")),
        "year": work.get("publication_year"),
        "venue": _venue(work),
        "doi": normalize_doi(work.get("doi")),
        "openalex_id": normalize_openalex_id(work.get("id")),
        "cited_by_count": work.get("cited_by_count") or 0,
        "type": work.get("type"),
        "language": work.get("language"),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        **_biblio(work),
        "is_retracted": bool(work.get("is_retracted")),
        "open_access_url": oa_url if isinstance(oa_url, str) else None,
    }


def paper_from_openalex(work: Mapping[str, Any]) -> Paper:
    return Paper(**_paper_kwargs_from_openalex(work))


def _detail_from_openalex(work: Mapping[str, Any]) -> PaperDetail:
    raw_refs = work.get("referenced_works")
    count = work.get("referenced_works_count")
    return PaperDetail(
        **_paper_kwargs_from_openalex(work),
        referenced_works_count=len(raw_refs) if isinstance(raw_refs, list) else (count or 0),
    )


def _s2_author_names(payload: Mapping[str, Any]) -> list[str]:
    authors = payload.get("authors")
    if not isinstance(authors, list):
        return []
    return [
        entry["name"].strip()
        for entry in authors
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"].strip()
    ]


def _s2_type(publication_types: Any) -> str | None:
    """Fold Semantic Scholar's type vocabulary onto OpenAlex's."""
    if not isinstance(publication_types, list) or not publication_types:
        return None
    raw = publication_types[0]
    if not isinstance(raw, str) or not raw:
        return None
    return _S2_TYPE_TO_OPENALEX.get(raw.lower(), raw.lower())


def _detail_from_semanticscholar(payload: Mapping[str, Any]) -> PaperDetail:
    external_ids = payload.get("externalIds")
    external_ids = external_ids if isinstance(external_ids, dict) else {}
    journal = payload.get("journal")
    journal = journal if isinstance(journal, dict) else {}
    corpus_id = external_ids.get("CorpusId")
    # S2 packs the page range into one "1259-1273" string where OpenAlex splits it.
    first_page, _, last_page = (journal.get("pages") or "").partition("-")
    open_access_pdf = payload.get("openAccessPdf")

    # A list comprehension cannot guard its own `for`, and `references` is simply
    # absent when the caller did not ask for the field -- so the shape check has to
    # happen before iterating, not inside the condition.
    raw_refs = payload.get("references")
    if not isinstance(raw_refs, list):
        raw_refs = []
    references: list[Reference] = []
    for entry in raw_refs:
        if not isinstance(entry, dict):
            continue
        ids = entry.get("externalIds")
        title = entry.get("title")
        references.append(
            Reference(
                title=title if isinstance(title, str) else None,
                year=entry.get("year"),
                doi=normalize_doi(ids.get("DOI")) if isinstance(ids, dict) else None,
            )
        )

    return PaperDetail(
        source="semanticscholar",
        title=payload.get("title") or "",
        authors=_s2_author_names(payload),
        year=payload.get("year"),
        venue=journal.get("name") or payload.get("venue") or None,
        doi=normalize_doi(external_ids.get("DOI")),
        corpus_id=int(corpus_id) if isinstance(corpus_id, int | float) else None,
        cited_by_count=payload.get("citationCount") or 0,
        type=_s2_type(payload.get("publicationTypes")),
        language=payload.get("language"),
        abstract=payload.get("abstract"),
        volume=journal.get("volume") or None,
        first_page=first_page or None,
        last_page=last_page or None,
        open_access_url=open_access_pdf.get("url")
        if isinstance(open_access_pdf, dict) and isinstance(open_access_pdf.get("url"), str)
        else None,
        references=references,
        referenced_works_count=payload.get("referenceCount") or len(references),
    )
