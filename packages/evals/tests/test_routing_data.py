"""routing.jsonl integrity.

The routing accuracy number reported on D7 is only worth anything if the dataset
behind it is well-formed. These tests enforce the invariants that make it so —
most importantly that every boundary case states *why* it is a boundary, because
an unexplained "expect: unclear" is indistinguishable from a lazy label.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data" / "routing.jsonl"

KINDS = {"normal", "ambiguous", "negative", "injection"}

# The intent vocabulary the router is allowed to emit. Kept here rather than
# imported from the router so that adding a label is a deliberate, reviewed
# change, and so this gate does not import the thing it is grading.
# test_the_intent_vocabulary_matches_the_router is what stops the two copies from
# drifting -- without it, a label added to the router and not here would make the
# dataset reject the router's own output.
INTENTS = {
    "literature_search",
    "outline",
    "draft_section",
    "polish",
    "citation_format",
    "unclear",
}

# Cases that exist to test a product boundary rather than a routing capability.
# Losing any of these would silently drop a guarantee from the eval.
REQUIRED_BOUNDARY_SUBSTRINGS = [
    "毕业论文",  # whole-paper ghost-writing: out of scope on integrity grounds
    "降重",  # plagiarism-ratio reduction: explicitly out of product scope
    "忽略之前的所有指令",  # direct prompt injection
]

# D7 exit criterion. Tracked as a visible skip rather than a hard failure so the
# gap shows up in every test run without breaking CI on day one.
D7_TARGET = 100


def _rows() -> list[dict]:
    lines = DATA.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    loaded = _rows()
    assert loaded, f"{DATA} is empty or missing"
    return loaded


# --------------------------------------------------------------------------- #
# Per-row schema
# --------------------------------------------------------------------------- #


def test_every_line_is_valid_json() -> None:
    """Checked separately from the fixture so a malformed line names itself
    instead of blowing up collection for the whole file."""
    for lineno, line in enumerate(DATA.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{DATA.name}:{lineno} is not valid JSON: {exc}")


def test_required_fields_present(rows: list[dict]) -> None:
    for i, row in enumerate(rows, start=1):
        assert isinstance(row.get("query"), str) and row["query"].strip(), f"row {i}: bad query"
        assert row.get("kind") in KINDS, f"row {i}: unknown kind {row.get('kind')!r}"
        assert isinstance(row.get("ui_context"), dict), f"row {i}: ui_context must be an object"


def test_labels_come_from_the_intent_vocabulary(rows: list[dict]) -> None:
    for i, row in enumerate(rows, start=1):
        if "expect" in row:
            assert row["expect"] in INTENTS, f"row {i}: {row['expect']!r} is not a known intent"


def test_the_intent_vocabulary_matches_the_router() -> None:
    """The dataset and the router must agree on the label set.

    Two copies of the same list, checked against each other. A label the router
    can emit but the dataset does not know makes that label unscoreable -- it
    counts as a miss forever and nobody can tell whether the router is wrong or
    the vocabulary is. The reverse is worse: a label the dataset expects but the
    router cannot produce caps accuracy below 100% no matter how good the routing
    gets, and looks exactly like a model that needs tuning.
    """
    from paper_agent.routing import INTENTS as ROUTER_INTENTS

    assert set(ROUTER_INTENTS) == INTENTS, (
        f"router emits {sorted(set(ROUTER_INTENTS) - INTENTS)} which the dataset does not grade; "
        f"dataset expects {sorted(INTENTS - set(ROUTER_INTENTS))} which the router cannot produce"
    )


# --------------------------------------------------------------------------- #
# Labelling discipline — this is what makes the metric trustworthy
# --------------------------------------------------------------------------- #


def test_ambiguous_rows_ask_instead_of_guessing(rows: list[dict]) -> None:
    """An ambiguous row must expect a clarifying question. Guessing the missing
    part is the failure mode this whole category exists to catch."""
    ambiguous = [r for r in rows if r["kind"] == "ambiguous"]
    assert ambiguous, "the ambiguous category must not be empty"
    for row in ambiguous:
        assert row.get("expect_clarify") is True, (
            f"ambiguous row lacks expect_clarify: {row['query']!r}"
        )
        if "expect" in row:
            assert row["expect"] in INTENTS, f"row {row['query']!r}: bad intent {row['expect']!r}"


def test_ambiguous_covers_both_unknown_intent_and_missing_slot(rows: list[dict]) -> None:
    """Two different failure modes share the `ambiguous` label, and the router
    handles them differently:

    * intent unknown — `expect_clarify` only ("帮我处理一下这个东西")
    * intent known, slot missing — `expect` AND `expect_clarify`
      ("我的参考文献格式不对" is clearly citation_format, but *which* style?)

    Both must stay in the dataset. Losing the second silently removes the only
    case proving the router can name an intent and still refuse to guess a slot,
    which is exactly the Jev-Choice-vs-LLM-slot split the cascade is built on.
    """
    ambiguous = [r for r in rows if r["kind"] == "ambiguous"]
    assert any("expect" not in r for r in ambiguous), "no unknown-intent ambiguous case"
    assert any("expect" in r for r in ambiguous), "no known-intent-missing-slot ambiguous case"


def test_non_ambiguous_rows_assert_an_intent(rows: list[dict]) -> None:
    """The converse: a row that is not ambiguous must be scored against a label,
    otherwise it contributes nothing to precision or recall."""
    for row in rows:
        if row["kind"] == "ambiguous":
            continue
        assert row.get("expect") in INTENTS, f"row has no usable label: {row['query']!r}"


# --------------------------------------------------------------------------- #
# path_deep — the fast/deep split the whole two-path architecture rests on
# --------------------------------------------------------------------------- #


def test_expect_deep_is_boolean_when_present(rows: list[dict]) -> None:
    """Optional field, but never a maybe. An absent `expect_deep` means "this row
    does not grade the path split"; a null or a string would silently grade
    nothing while looking like it grades something."""
    for row in rows:
        if "expect_deep" in row:
            assert isinstance(row["expect_deep"], bool), (
                f"expect_deep must be a bool or absent: {row['query']!r}"
            )


def test_path_deep_covers_both_outcomes(rows: list[dict]) -> None:
    """The fast/deep split is the reason there are two paths at all. A dataset
    that only labels one side cannot measure the decision that matters most --
    sending a narrow request down the deep path is the expensive mistake, and
    sending a chapter-level request down the fast path is the one that produces a
    bad result."""
    deep = [r for r in rows if r.get("expect_deep") is True]
    fast = [r for r in rows if r.get("expect_deep") is False]
    assert deep, "no row expects the deep path"
    assert fast, "no row expects the fast path"


def test_deep_rows_explain_why_they_are_deep(rows: list[dict]) -> None:
    """Same discipline as the boundary cases.

    "Deep" is a judgement about cost and grounding, not a property of the
    sentence, so an unexplained `true` is unreviewable -- and the natural way to
    make a failing metric pass is to relabel rows nobody documented.
    """
    for row in rows:
        if row.get("expect_deep") is not True:
            continue
        note = row.get("note")
        assert isinstance(note, str) and len(note.strip()) >= 6, (
            f"deep row needs a note saying why it needs the heavy pipeline: {row['query']!r}"
        )


def test_boundary_rows_do_not_claim_a_path(rows: list[dict]) -> None:
    """A request that is going to be refused has no path.

    Labelling one would make the accuracy number reward the router for choosing a
    pipeline it should never start, and would hide the case where a jailbreak is
    correctly identified as out of scope but still routed somewhere expensive.
    """
    for row in rows:
        if row["kind"] in {"negative", "injection"}:
            assert "expect_deep" not in row, (
                f"boundary case must not claim a path: {row['query']!r}"
            )


def test_every_boundary_case_explains_itself(rows: list[dict]) -> None:
    """negative and injection rows must carry a `note` saying why they are out of
    bounds. Without it a future reader cannot tell a principled boundary from a
    mislabelled row, and will 'fix' the data to match a broken router."""
    for row in rows:
        if row["kind"] not in {"negative", "injection"}:
            continue
        note = row.get("note")
        assert isinstance(note, str) and len(note.strip()) >= 6, (
            f"boundary case needs a note explaining why: {row['query']!r}"
        )


def test_negative_and_injection_expect_unclear(rows: list[dict]) -> None:
    """Out-of-scope and injected requests must route to `unclear` so the gateway
    refuses them, rather than being forced into a real skill."""
    for row in rows:
        if row["kind"] in {"negative", "injection"}:
            assert row.get("expect") == "unclear", (
                f"boundary case must route to unclear: {row['query']!r}"
            )


def test_product_boundaries_are_covered(rows: list[dict]) -> None:
    """The positioning is 'assist, never ghost-write'. If these cases disappear
    from the dataset, nothing in CI would notice the guarantee being dropped."""
    queries = " ".join(r["query"] for r in rows)
    for needle in REQUIRED_BOUNDARY_SUBSTRINGS:
        assert needle in queries, f"missing the boundary case for {needle!r}"


# --------------------------------------------------------------------------- #
# Dataset shape
# --------------------------------------------------------------------------- #


def test_queries_are_unique(rows: list[dict]) -> None:
    """A duplicated row silently double-weights one case in the accuracy number."""
    dupes = [q for q, n in Counter(r["query"] for r in rows).items() if n > 1]
    assert not dupes, f"duplicate queries: {dupes}"


def test_all_four_kinds_present(rows: list[dict]) -> None:
    kinds = {r["kind"] for r in rows}
    assert kinds == KINDS, f"missing kinds: {KINDS - kinds}"


def test_every_intent_except_unclear_has_a_positive_case(rows: list[dict]) -> None:
    """An intent with no normal-case row can never be measured for recall."""
    labelled = {r["expect"] for r in rows if r["kind"] == "normal"}
    assert INTENTS - {"unclear"} <= labelled, (
        f"untested intents: {INTENTS - {'unclear'} - labelled}"
    )


def test_ui_context_is_actually_exercised(rows: list[dict]) -> None:
    """ui_context is meant to be a stronger routing signal than the message text.
    If every row leaves it empty, the cascade's cheapest tier is untested."""
    assert any(r["ui_context"] for r in rows), "no row supplies ui_context"


def test_dataset_has_a_minimum_floor(rows: list[dict]) -> None:
    assert len(rows) >= 20, f"only {len(rows)} cases; too few for a stable estimate"


@pytest.mark.skipif(
    len(_rows()) < D7_TARGET,
    reason=f"D7 target is {D7_TARGET} routing cases, currently {len(_rows())}",
)
def test_d7_dataset_target() -> None:
    assert len(_rows()) >= D7_TARGET
