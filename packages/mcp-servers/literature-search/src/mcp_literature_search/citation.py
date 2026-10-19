"""Three citation styles, hand-rolled.

Not `citeproc-py`. A CSL processor needs the style's XML at runtime, which means
either vendoring three CSL files or fetching them over the network from a tool
whose whole job is to format a reference offline and deterministically -- and
coverage of GB/T 7714 in the CSL ecosystem is poor, which is the one style this
product needs most, because it writes for Chinese journals. Three renderers over
one normalized record is about a hundred lines with no data dependency, and every
line is a pure function of its input, so `tests/test_citation.py` is the spec.

**Three known deviations, all deliberate.**

*Titles are not case-converted.* APA wants sentence case and the upstreams supply
title case. Lowercasing algorithmically would turn "BERT" into "Bert" and
"CRISPR" into "Crispr", and a mangled proper noun in a bibliography is worse than
the wrong capitalization -- it is a factual error about the paper's name.

*Surname detection assumes the last token is the family name.* That is right for
the Latinized names both APIs return and wrong for particles -- "Ludwig van
Beethoven" renders as "Beethoven, L. V.", with the particle read as a middle
initial -- and for any name recorded surname-first. Neither upstream exposes a
structured surname field, so there is nothing better to key off. Recorded here
rather than in a TODO because the failure is silent and looks like a typo in the
output; `tests/test_citation.py` pins the wrong answer so fixing it is a
deliberate change rather than a surprise.

*A missing year is omitted, not replaced by a placeholder.* GB/T 7714 prescribes
`[出版年不详]` for the empty year position. In a reference list assembled by a
tool, that string reads as generated noise and invites a reader to think the
formatting is broken; leaving the position empty leaves a visible gap instead, and
the record's `year: null` already tells the agent the data is missing.
"""

from __future__ import annotations

from mcp_literature_search.models import CitationStyle, Paper

#: GB/T 7714-2015 文献类型标识, keyed on the OpenAlex `type` vocabulary that
#: `upstream._S2_TYPE_TO_OPENALEX` folds Semantic Scholar into. One vocabulary
#: keeps this table from having to be maintained twice.
_GB_TYPE_MARKER = {
    "article": "J",
    "journal-article": "J",
    "review": "J",
    "editorial": "J",
    "letter": "J",
    "survey": "J",
    "study": "J",
    "conference-paper": "C",
    "proceedings-article": "C",
    "component": "C",
    "book": "M",
    "book-chapter": "M",
    "monograph": "M",
    "reference-entry": "M",
    "dissertation": "D",
    "thesis": "D",
    "report": "R",
    "library-guide": "R",
    "standard": "S",
    "dataset": "DS",
    "software": "CP/OL",
    "preprint": "EB/OL",
    "posted-content": "EB/OL",
    "paratext": "EB/OL",
}

#: Apa and IEEE set page ranges with an en dash; GB/T 7714 uses a hyphen. Small,
#: and exactly the kind of thing a copy editor circles.
_EN_DASH = "\u2013"
_HYPHEN = "-"


def _split_name(display_name: str) -> tuple[str, str]:
    """`"Zhaoyang Liu"` -> `("Liu", "Zhaoyang")`. See the module docstring."""
    parts = display_name.split()
    if len(parts) <= 1:
        return display_name.strip(), ""
    return parts[-1], " ".join(parts[:-1])


def _initials(given: str) -> list[str]:
    """First letter of each given-name token.

    Handles both spellings the upstreams produce -- `"Zhaoyang"` and `"J. R."` --
    by dropping punctuation before splitting, so a name already reduced to
    initials does not come back empty.
    """
    tokens = given.replace(".", " ").replace("-", " ").split()
    return [token[0].upper() for token in tokens if token[:1].isalpha()]


def _apa_name(display_name: str) -> str:
    """`Xu Xie` -> `Xie, X.`"""
    surname, given = _split_name(display_name)
    initials = _initials(given)
    if not initials:
        return surname
    return f"{surname}, " + " ".join(f"{letter}." for letter in initials)


def _ieee_name(display_name: str) -> str:
    """`Xu Xie` -> `X. Xie`"""
    surname, given = _split_name(display_name)
    initials = _initials(given)
    if not initials:
        return surname
    return " ".join(f"{letter}." for letter in initials) + f" {surname}"


def _gb_name(display_name: str) -> str:
    """`Zhaoyang Liu` -> `LIU Z Y`.

    GB/T 7714 wants the surname in full capitals and the given name as
    space-separated initials with no punctuation.
    """
    surname, given = _split_name(display_name)
    initials = _initials(given)
    if not initials:
        return surname.upper()
    return f"{surname.upper()} {' '.join(initials)}"


def _apa_authors(record: Paper) -> str:
    formatted = [_apa_name(name) for name in record.authors]
    if not formatted:
        return ""
    if len(formatted) == 1:
        return formatted[0]
    # APA 7th: ampersand before the last name up to 20 authors; from 21, the first
    # 19, an ellipsis, then the final author with no ampersand.
    if len(formatted) <= 20:
        return ", ".join(formatted[:-1]) + ", & " + formatted[-1]
    return ", ".join(formatted[:19]) + ", ... " + formatted[-1]


def _ieee_authors(record: Paper) -> str:
    formatted = [_ieee_name(name) for name in record.authors]
    if not formatted:
        return ""
    if len(formatted) == 1:
        return formatted[0]
    if len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    # IEEE: a serial comma for three to six, "et al." beyond.
    if len(formatted) <= 6:
        return ", ".join(formatted[:-1]) + ", and " + formatted[-1]
    return f"{formatted[0]} et al."


def _gb_authors(record: Paper) -> str:
    formatted = [_gb_name(name) for name in record.authors]
    if not formatted:
        return ""
    if len(formatted) <= 3:
        return ", ".join(formatted)
    # 等 for a Chinese-language work, et al otherwise. The choice follows the
    # language of the cited work rather than of the user, because the marker is
    # part of the reference as it will be printed.
    suffix = "等" if (record.language or "").lower().startswith("zh") else "et al"
    return ", ".join(formatted[:3]) + f", {suffix}"


def _pages(record: Paper, dash: str) -> str | None:
    if record.first_page and record.last_page:
        return f"{record.first_page}{dash}{record.last_page}"
    return record.first_page or record.last_page


def _doi_url(record: Paper) -> str | None:
    return f"https://doi.org/{record.doi}" if record.doi else None


def _terminate(text: str) -> str:
    """End a segment with one period, never two.

    Needed because APA and IEEE abbreviate given names, so an author list already
    ends in a period before the sentence needs one -- `Xie, X.` plus the segment's
    period is `Xie, X..`, which reads as a typo in the one place a reader is
    looking closely.
    """
    return text if text.endswith(".") else text + "."


def _sentence(title: str) -> str:
    """End the title with a period without doubling one.

    A title already ending in `?` or `!` keeps that mark: APA does not add a
    period after terminal question or exclamation marks.
    """
    text = title.strip()
    if text.endswith(("?", "!")):
        return text
    return _terminate(text)


def _apa(record: Paper) -> str:
    """`Author, A. (2022). Title. Venue, 12(3), 100–110. https://doi.org/10.x`"""
    parts: list[str] = []
    authors = _apa_authors(record)
    if authors:
        parts.append(_terminate(authors))
    parts.append(f"({record.year or 'n.d.'}).")
    parts.append(_sentence(record.title))

    # Venue, volume(issue) and pages are one comma-separated sentence in APA, not
    # three, so they are collected and joined rather than appended in sequence.
    source = [value for value in (record.venue, _apa_volume_issue(record)) if value]
    pages = _pages(record, _EN_DASH)
    if pages:
        source.append(pages)
    if source:
        parts.append(", ".join(source) + ".")

    url = _doi_url(record) or record.open_access_url
    if url:
        parts.append(f"{url}.")
    return " ".join(parts)


def _apa_volume_issue(record: Paper) -> str | None:
    if record.volume and record.issue:
        return f"{record.volume}({record.issue})"
    return record.volume


def _ieee(record: Paper) -> str:
    """`A. Xie et al., "Title," in Venue, vol. 12, pp. 100–110, 2022, doi: 10.x.`"""
    parts: list[str] = []
    authors = _ieee_authors(record)
    if authors:
        parts.append(f"{authors},")
    parts.append(f'"{record.title.strip().rstrip(".")},"')
    if record.venue:
        # IEEE puts `in` before a proceedings title and not before a journal title,
        # which is the one place the two shapes differ.
        prefix = "in " if (record.type or "").endswith("paper") else ""
        parts.append(f"{prefix}{record.venue},")
    if record.volume:
        parts.append(f"vol. {record.volume},")
    if record.issue:
        parts.append(f"no. {record.issue},")
    pages = _pages(record, _EN_DASH)
    if pages:
        parts.append(f"pp. {pages},")
    if record.year:
        parts.append(f"{record.year},")
    if record.doi:
        parts.append(f"doi: {record.doi}.")
    elif record.open_access_url:
        parts.append(f"{record.open_access_url}.")

    text = " ".join(parts)
    if text.endswith(',"'):
        # Nothing followed the title, so its closing period goes inside the quotes.
        return text[:-2] + '."'
    if text.endswith(","):
        return text[:-1] + "."
    return text if text.endswith(".") else text + "."


def _gb(record: Paper) -> str:
    """GB/T 7714-2015 顺序编码制."""
    marker = _GB_TYPE_MARKER.get((record.type or "").lower(), "Z")
    pages = _pages(record, _HYPHEN)
    title = record.title.strip()

    if marker == "J":
        body = _gb_journal(record, title, pages)
    elif marker == "C":
        body = _gb_conference(record, title, pages)
    else:
        body = _gb_generic(record, title, marker)

    authors = _gb_authors(record)
    citation = f"{authors}. {body}" if authors else body
    if record.doi:
        citation += f" DOI:{record.doi}."
    return citation


def _gb_journal(record: Paper, title: str, pages: str | None) -> str:
    """`题名[J]. 刊名, 年, 卷(期): 页码.`"""
    volume = None
    if record.volume:
        volume = record.volume + (f"({record.issue})" if record.issue else "")
    fields = [value for value in (record.venue, _gb_year(record.year), volume) if value]
    body = f"{title}[J]."
    if fields:
        body += " " + ", ".join(fields)
    if pages:
        body += f": {pages}"
    return body + "."


def _gb_conference(record: Paper, title: str, pages: str | None) -> str:
    """`题名[C]//论文集名. 年: 页码.`

    The standard's full form is `[C]//论文集名. 出版地: 出版者, 年: 页码.` Neither
    upstream supplies place or publisher, so those positions are left out rather
    than padded -- the double slash is what marks the source as a proceedings.
    """
    body = f"{title}[C]//{record.venue}." if record.venue else f"{title}[C]."
    year = _gb_year(record.year)
    if year:
        body += f" {year}"
        body += f": {pages}." if pages else "."
    elif pages:
        body += f" {pages}."
    return body if body.endswith(".") else body + "."


def _gb_generic(record: Paper, title: str, marker: str) -> str:
    """Monographs, theses, reports, standards and online resources.

    All of these share GB/T's basic shape -- `题名[标识]. 出版者, 年.` -- and the
    online ones add a URL, so one renderer covers M / D / R / S / DS / CP-OL /
    EB-OL / Z.
    """
    body = f"{title}[{marker}]."
    fields = [value for value in (record.venue, _gb_year(record.year)) if value]
    if fields:
        body += " " + ", ".join(fields) + "."
    if marker in ("EB/OL", "CP/OL", "DS"):
        url = record.open_access_url or _doi_url(record)
        if url:
            body += f" {url}."
    return body


def _gb_year(year: int | None) -> str | None:
    return str(year) if year else None


def format_citation(record: Paper, style: CitationStyle) -> str:
    """Render one record in one style.

    Pure: no network, no clock, no locale. Given the same record it returns the
    same string, which is what makes `tests/test_citation.py` a specification
    rather than a snapshot of whatever the upstreams happened to return today.
    """
    if style == "apa":
        return _apa(record)
    if style == "ieee":
        return _ieee(record)
    if style == "gb-t-7714":
        return _gb(record)
    raise ValueError(f"unsupported citation style {style!r}")
