"""The three citation styles, as a specification.

The exact-match assertions here are deliberate: a bibliography is text a reader
checks character by character, so "contains the DOI" is not a strong enough
contract. The first three tests pin the renderings of a real record --
`W3133849783`, captured live from OpenAlex -- so that a change to any helper
shows up as a diff in an actual reference entry rather than as a green suite.
"""

from __future__ import annotations

import pytest
from mcp_literature_search.citation import format_citation
from mcp_literature_search.models import Paper
from mcp_literature_search.upstream import paper_from_openalex

EN_DASH = "\u2013"


@pytest.fixture
def real(detail_payload) -> Paper:
    """The captured conference paper, normalized the same way a tool call would."""
    return paper_from_openalex(detail_payload)


def journal(**overrides: object) -> Paper:
    """A complete journal article, so each test can remove exactly one field."""
    record = Paper(
        source="openalex",
        title="A Study of Something",
        authors=["Ada Lovelace", "Alan Turing"],
        year=2020,
        venue="Journal of Examples",
        doi="10.1000/example.2020",
        type="article",
        language="en",
        volume="12",
        issue="3",
        first_page="100",
        last_page="110",
    )
    return record.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# The real record, all three styles
# ---------------------------------------------------------------------------


def test_apa_on_the_real_record(real) -> None:
    assert format_citation(real, "apa") == (
        "Xie, X., Sun, F., Liu, Z., Wu, S., Gao, J., Zhang, J., Ding, B., & Cui, B. (2022). "
        "Contrastive Learning for Sequential Recommendation. Proceedings - International "
        f"Conference on Data Engineering, 1259{EN_DASH}1273. "
        "https://doi.org/10.1109/icde53745.2022.00099."
    )


def test_ieee_on_the_real_record(real) -> None:
    assert format_citation(real, "ieee") == (
        'X. Xie et al., "Contrastive Learning for Sequential Recommendation," in '
        f"Proceedings - International Conference on Data Engineering, pp. 1259{EN_DASH}1273, "
        "2022, doi: 10.1109/icde53745.2022.00099."
    )


def test_gb_on_the_real_record(real) -> None:
    # `LIU Z`, not `LIU Z Y`: "Zhaoyang" is one token upstream, and there is no
    # structured given-name field to split it with. Pinned here so the behaviour is
    # a known deviation rather than a surprise.
    assert format_citation(real, "gb-t-7714") == (
        "XIE X, SUN F, LIU Z, et al. Contrastive Learning for Sequential "
        "Recommendation[C]//Proceedings - International Conference on Data "
        "Engineering. 2022: 1259-1273. DOI:10.1109/icde53745.2022.00099."
    )


def test_ieee_puts_in_before_a_proceedings_but_not_before_a_journal(real) -> None:
    assert '" in Proceedings' in format_citation(real, "ieee")
    # The comma belongs to the quoted title, so the journal form is `" Journal`.
    assert '" Journal of Examples,' in format_citation(journal(type="article"), "ieee")


# ---------------------------------------------------------------------------
# Author lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count,expected", [(1, "A. One"), (2, "A. One and B. Two")])
def test_ieee_short_author_lists(count: int, expected: str) -> None:
    names = ["Ada One", "Bert Two"][:count]
    text = format_citation(journal(authors=names), "ieee")
    assert text.startswith(f"{expected}, ")


def test_ieee_uses_a_serial_comma_up_to_six() -> None:
    names = [f"Given{i} Family{i}" for i in range(6)]
    text = format_citation(journal(authors=names), "ieee")
    assert text.startswith(
        "G. Family0, G. Family1, G. Family2, G. Family3, G. Family4, and G. Family5, "
    )


def test_ieee_collapses_at_seven() -> None:
    names = [f"Given{i} Family{i}" for i in range(7)]
    assert format_citation(journal(authors=names), "ieee").startswith("G. Family0 et al., ")


def test_apa_ampersand_before_the_last_author() -> None:
    names = ["Ada One", "Bert Two", "Cy Three"]
    assert format_citation(journal(authors=names), "apa").startswith(
        "One, A., Two, B., & Three, C. (2020)."
    )


def test_apa_ellipsis_rule_kicks_in_at_twenty_one() -> None:
    """APA 7th: 20 or fewer are all listed; from 21, the first 19, an ellipsis, the last."""
    nineteen = [f"Given{i} Family{i}" for i in range(19)]
    twenty = nineteen + ["Zeta Author"]
    twenty_one = twenty + ["Very Final"]

    text = format_citation(journal(authors=twenty), "apa")
    assert ", & Author, Z." in text
    assert "..." not in text

    text = format_citation(journal(authors=twenty_one), "apa")
    assert ", ... Final, V." in text
    assert "&" not in text
    # The twentieth author is the one the rule drops, which is what makes 21
    # different from 20 rather than merely longer.
    assert "Author, Z." not in text
    assert "Family18, G." in text


def test_gb_lists_three_then_truncates() -> None:
    four = ["Ada One", "Bert Two", "Cy Three", "Di Four"]
    assert format_citation(journal(authors=four), "gb-t-7714").startswith(
        "ONE A, TWO B, THREE C, et al. "
    )
    three = four[:3]
    assert format_citation(journal(authors=three), "gb-t-7714").startswith(
        "ONE A, TWO B, THREE C. "
    )


def test_gb_uses_the_chinese_marker_for_a_chinese_work() -> None:
    four = ["Ada One", "Bert Two", "Cy Three", "Di Four"]
    text = format_citation(journal(authors=four, language="zh"), "gb-t-7714")
    assert ", 等. " in text
    assert "et al" not in text


def test_a_single_token_name_has_no_initial() -> None:
    """Upstreams sometimes carry a mononym. Better bare than `Plato, .`"""
    record = journal(authors=["Plato"])
    assert format_citation(record, "apa").startswith("Plato. (2020).")
    assert format_citation(record, "ieee").startswith("Plato, ")
    assert format_citation(record, "gb-t-7714").startswith("PLATO. ")


def test_given_names_already_reduced_to_initials_survive() -> None:
    record = journal(authors=["J. R. Smith"])
    assert format_citation(record, "apa").startswith("Smith, J. R. (2020).")
    assert format_citation(record, "gb-t-7714").startswith("SMITH J R. ")


def test_no_authors_at_all() -> None:
    """Starts at the year rather than emitting a leading period."""
    text = format_citation(journal(authors=[]), "apa")
    assert text.startswith("(2020). A Study of Something.")
    assert format_citation(journal(authors=[]), "gb-t-7714").startswith("A Study of Something[J].")


# ---------------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------------


def test_missing_year_renders_n_d_in_apa_and_a_gap_in_gb() -> None:
    """Documented deviation: GB/T's `[出版年不详]` is omitted, not substituted."""
    assert "(n.d.)." in format_citation(journal(year=None), "apa")
    text = format_citation(journal(year=None), "gb-t-7714")
    assert "[出版年不详]" not in text
    assert "Journal of Examples, 12(3): 100-110." in text


def test_missing_pages_uses_a_single_page_when_that_is_all_there_is() -> None:
    assert "pp. 100, 2020" in format_citation(journal(last_page=None), "ieee")
    assert ", 100." in format_citation(journal(last_page=None), "apa")
    assert ": 100." in format_citation(journal(last_page=None), "gb-t-7714")


def test_missing_venue_and_pages_still_produces_a_citation() -> None:
    """Volume and issue survive on their own; APA still comma-joins what is left."""
    text = format_citation(journal(venue=None, first_page=None, last_page=None), "apa")
    assert text == (
        "Lovelace, A., & Turing, A. (2020). A Study of Something. 12(3). "
        "https://doi.org/10.1000/example.2020."
    )


def test_volume_without_issue() -> None:
    assert "Journal of Examples, 12, 100" in format_citation(journal(issue=None), "apa")
    assert "vol. 12, no." not in format_citation(journal(issue=None), "ieee")
    assert "Journal of Examples, 2020, 12: 100-110." in format_citation(
        journal(issue=None), "gb-t-7714"
    )


def test_no_doi_falls_back_to_the_open_access_url() -> None:
    record = journal(doi=None, open_access_url="https://arxiv.org/pdf/2010.14395")
    assert format_citation(record, "apa").endswith("https://arxiv.org/pdf/2010.14395.")
    assert format_citation(record, "ieee").endswith("https://arxiv.org/pdf/2010.14395.")
    assert "arxiv.org" not in format_citation(record, "gb-t-7714")


def test_no_doi_and_no_url_leaves_nothing_to_link() -> None:
    text = format_citation(journal(doi=None), "apa")
    assert text.endswith("100\u2013110.")
    assert "https://doi.org" not in text


# ---------------------------------------------------------------------------
# Punctuation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Does It Work?", "Does It Work?"),
        ("It Works!", "It Works!"),
        ("Already Periodic.", "Already Periodic."),
        ("Plain Title", "Plain Title."),
    ],
)
def test_apa_does_not_double_terminal_punctuation(title: str, expected: str) -> None:
    assert f"(2020). {expected} Journal" in format_citation(journal(title=title), "apa")


def test_ieee_strips_a_title_period_before_the_closing_quote() -> None:
    assert '"Already Periodic,"' in format_citation(journal(title="Already Periodic."), "ieee")


def test_no_style_emits_a_doubled_period() -> None:
    """The whole point of `_terminate`, checked as an invariant.

    The 21-author APA case is excluded on purpose: its ellipsis is a literal `...`.
    """
    records = [
        journal(),
        journal(authors=["Ada Lovelace"]),
        journal(authors=[], year=None, venue=None, doi=None),
        journal(title="Does It Work?", type="book"),
        journal(type="preprint", open_access_url="https://example.org/x"),
    ]
    for record in records:
        for style in ("apa", "ieee", "gb-t-7714"):
            text = format_citation(record, style)
            assert ".." not in text, f"{style}: {text}"
            assert text.endswith("."), f"{style} does not end in a period: {text}"
            assert "  " not in text, f"{style} has a double space: {text}"


# ---------------------------------------------------------------------------
# GB/T document-type markers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_,marker",
    [
        ("article", "[J]"),
        ("review", "[J]"),
        ("conference-paper", "[C]"),
        ("book", "[M]"),
        ("book-chapter", "[M]"),
        ("dissertation", "[D]"),
        ("report", "[R]"),
        ("standard", "[S]"),
        ("dataset", "[DS]"),
        ("software", "[CP/OL]"),
        ("preprint", "[EB/OL]"),
    ],
)
def test_gb_marker_follows_the_document_type(type_: str, marker: str) -> None:
    text = format_citation(journal(type=type_), "gb-t-7714")
    assert marker in text


def test_gb_falls_back_to_the_undefined_marker() -> None:
    """`[Z]` is honest about not knowing; guessing a marker implies a format we did not verify."""
    assert "[Z]" in format_citation(journal(type="something-new"), "gb-t-7714")
    assert "[Z]" in format_citation(journal(type=None), "gb-t-7714")


def test_gb_conference_uses_the_double_slash_only_with_a_proceedings_name() -> None:
    text = format_citation(journal(type="conference-paper", venue=None), "gb-t-7714")
    assert "A Study of Something[C]. 2020" in text
    assert "//" not in text


def test_gb_online_types_carry_the_url() -> None:
    record = journal(type="preprint", open_access_url="https://example.org/x", venue=None)
    text = format_citation(record, "gb-t-7714")
    assert "[EB/OL]." in text
    assert text.endswith("https://example.org/x. DOI:10.1000/example.2020.")


def test_gb_journal_shape() -> None:
    assert format_citation(journal(), "gb-t-7714") == (
        "LOVELACE A, TURING A. A Study of Something[J]. Journal of Examples, 2020, "
        "12(3): 100-110. DOI:10.1000/example.2020."
    )


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_an_unsupported_style_raises_rather_than_guessing() -> None:
    """Unreachable through the tool -- pydantic validates `CitationStyle` first."""
    with pytest.raises(ValueError, match="harvard"):
        format_citation(journal(), "harvard")  # type: ignore[arg-type]


def test_formatting_is_pure() -> None:
    """Same record in, same string out, twice -- no clock, no locale, no state."""
    record = journal()
    assert format_citation(record, "apa") == format_citation(record, "apa")


def test_titles_are_not_case_converted() -> None:
    """Documented deviation: lowercasing would turn BERT into Bert."""
    record = journal(title="BERT: Pre-training of Deep Bidirectional Transformers")
    assert "BERT: Pre-training" in format_citation(record, "apa")


def test_a_particle_surname_is_a_known_wrong_answer() -> None:
    """Documented deviation, pinned so a future fix is a deliberate change.

    The particle is not merely misplaced, it is consumed as a middle initial --
    "van" becomes "V.". Neither upstream supplies a structured surname, so there is
    no better field to key off; `citation.py`'s docstring records this.
    """
    record = journal(authors=["Ludwig van Beethoven"])
    assert format_citation(record, "apa").startswith("Beethoven, L. V. (2020).")
    assert format_citation(record, "gb-t-7714").startswith("BEETHOVEN L V. ")
