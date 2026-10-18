"""The routing abstraction: vocabulary, Noul binarization, keys, serialization.

These are the pieces both backends share, so a mistake here is a mistake in every
routing path at once. The vocabulary tests matter most: `INTENTS` is duplicated
in `packages/evals/tests/test_routing_data.py` on purpose, and duplication that
nothing checks is duplication that drifts.
"""

from __future__ import annotations

import json
from typing import get_args

import pytest
from paper_agent.routing.decision_model import (
    INTENTS,
    ROUTE_SOURCES,
    Decisions,
    Intent,
    RouteInput,
    RouteSource,
    coerce_intent,
    decisions_from_json,
    decisions_to_json,
    noul_to_bool,
    static_fallback,
)
from paper_agent.routing.resilience import QUESTION_FINGERPRINT, decision_key

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


def test_intents_tuple_matches_the_literal() -> None:
    """`INTENTS` is the runtime value of the `Intent` type.

    A Literal cannot be membership-tested, so the tuple exists -- and the two can
    disagree the moment someone adds a label to one and not the other. This is
    the test that makes adding an intent a two-line change instead of a silent
    one.
    """
    assert set(INTENTS) == set(get_args(Intent))
    assert len(INTENTS) == len(set(INTENTS)), "duplicate intent in the tuple"


def test_route_sources_tuple_matches_the_literal() -> None:
    assert set(ROUTE_SOURCES) == set(get_args(RouteSource))


def test_unclear_is_the_only_intent_that_is_also_the_fallback() -> None:
    """`unclear` must stay in the vocabulary: every degradation path routes to
    it, and removing it would leave `coerce_intent` returning a label that no
    conditional edge matches."""
    assert "unclear" in INTENTS
    assert static_fallback().intent == "unclear"


# --------------------------------------------------------------------------- #
# Noul binarization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("probability", "uncertain", "expected"),
    [
        (0.98, False, True),
        (0.98, True, True),
        (0.02, False, False),
        (0.02, True, False),
        # Inside the band: the model did not decide, so the caller's default wins.
        (0.5, False, False),
        (0.5, True, True),
        (0.55, True, True),
        (0.45, False, False),
        # Just outside the band (0.10), where the probability decides again.
        (0.61, False, True),
        (0.39, True, False),
        # A missing answer takes the same path as an undecided one.
        (None, True, True),
        (None, False, False),
    ],
)
def test_noul_to_bool(probability: float | None, uncertain: bool, expected: bool) -> None:
    assert noul_to_bool(probability, uncertain=uncertain) is expected


def test_the_two_questions_default_in_opposite_directions() -> None:
    """The asymmetry is the design, not an accident of two call sites.

    An undecided `needs_clarify` asks the user; an undecided `path_deep` stays on
    the fast path. Both are the cheap mistake. If someone "simplifies" this into
    one shared default, this test is what tells them the two cases were different
    on purpose.
    """
    assert noul_to_bool(None, uncertain=True) is True
    assert noul_to_bool(None, uncertain=False) is False


def test_band_boundaries_are_inclusive() -> None:
    """`<=` not `<`: exactly at the edge counts as undecided. Verified against
    the constant so widening the band does not silently change this."""
    from paper_agent.routing.decision_model import NOUL_THRESHOLD, NOUL_UNCERTAIN_BAND

    edge = NOUL_THRESHOLD + NOUL_UNCERTAIN_BAND
    assert noul_to_bool(edge, uncertain=True) is True
    assert noul_to_bool(edge, uncertain=False) is False
    assert noul_to_bool(edge + 1e-9, uncertain=True) is True


# --------------------------------------------------------------------------- #
# coerce_intent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("intent", INTENTS)
def test_every_vocabulary_label_survives_coercion(intent: str) -> None:
    assert coerce_intent(intent) == intent


@pytest.mark.parametrize(
    "value",
    [
        "literature-search",  # hyphen instead of underscore
        "search_literature",  # word order
        "Polish",  # wrong case
        "",
        "  ",
        None,
        42,
        ["polish"],
        {"intent": "polish"},
    ],
)
def test_anything_outside_the_vocabulary_becomes_unclear(value: object) -> None:
    assert coerce_intent(value) == "unclear"


def test_surrounding_whitespace_is_tolerated() -> None:
    """A model that emits `"polish\\n"` gave the right answer, and rejecting it
    would turn a correct routing into a clarifying question."""
    assert coerce_intent("  polish\n") == "polish"


# --------------------------------------------------------------------------- #
# RouteInput
# --------------------------------------------------------------------------- #


def test_state_always_includes_ui_context_even_when_empty() -> None:
    """An omitted key reads as a signal; an empty one reads as "no signal".

    The distinction matters because a backend that learns "no ui_context key
    means the user is in the editor" has learned something that is not true.
    """
    assert RouteInput(query="x").as_state() == {"query": "x", "ui_context": {}}
    assert RouteInput(query="x", ui_context={"active_panel": "outline"}).as_state() == {
        "query": "x",
        "ui_context": {"active_panel": "outline"},
    }


def test_route_input_is_immutable() -> None:
    request = RouteInput(query="x")
    with pytest.raises(AttributeError):
        request.query = "y"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Cache key
# --------------------------------------------------------------------------- #


def test_decision_key_is_a_pinned_sha256_not_the_builtin_hash() -> None:
    """Golden value, deliberately brittle.

    `hash()` of a str is salted per process, so a key built from it differs
    between workers and across restarts: a shared Redis would never hit, and a
    local single-process test would still pass. Nothing about that failure is
    observable except through the exact bytes, which is why the expected digest
    is written out here. If this test breaks, the key derivation changed --
    update the golden value only after accepting that every cached decision is
    now unreachable.
    """
    key = decision_key(
        RouteInput(query="帮我找几篇论文", ui_context={"active_panel": "references"})
    )
    assert key == "route:357bc05dbdf9c45e7558898dcc4386636f0cb404cd0f753b48106d3c5c9e747c"
    assert len(key) == len("route:") + 64


def test_ui_context_key_order_does_not_change_the_key() -> None:
    """Two clients sending the same context in a different order are asking the
    same question. Without `sort_keys` this is a cache miss, and it looks like a
    cache that simply does not work."""
    a = decision_key(
        RouteInput(query="q", ui_context={"active_panel": "editor", "cursor_section": "3.2"})
    )
    b = decision_key(
        RouteInput(query="q", ui_context={"cursor_section": "3.2", "active_panel": "editor"})
    )
    assert a == b


def test_different_queries_get_different_keys() -> None:
    a = decision_key(RouteInput(query="帮我找几篇论文"))
    b = decision_key(RouteInput(query="帮我润色这一段"))
    assert a != b


def test_ui_context_participates_in_the_key() -> None:
    """The same message means different things in different panels, so caching
    across them would serve a decision made about a different context."""
    bare = decision_key(RouteInput(query="把它弄好一点"))
    with_panel = decision_key(
        RouteInput(query="把它弄好一点", ui_context={"active_panel": "references"})
    )
    assert bare != with_panel


def test_editing_the_question_set_invalidates_every_cached_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The criteria *are* the prompt.

    Tuning them against the eval set changes what the router means by "polish",
    so a decision cached under the old wording is an answer to a different
    question. Folding the fingerprint into the key makes that invalidate itself;
    without it the change appears to do nothing for up to a TTL, which sends
    whoever made it debugging the wrong layer.
    """
    from paper_agent.routing import resilience

    request = RouteInput(query="把这段改好一点")
    before = decision_key(request)
    monkeypatch.setattr(resilience, "QUESTION_FINGERPRINT", "0" * 12)
    assert decision_key(request) != before


def test_the_question_fingerprint_is_a_stable_short_digest() -> None:
    assert len(QUESTION_FINGERPRINT) == 12
    assert all(c in "0123456789abcdef" for c in QUESTION_FINGERPRINT)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def _sample() -> Decisions:
    return Decisions(
        intent="citation_format",
        needs_clarify=True,
        path_deep=False,
        confidence=0.72,
        source="jev",
        probabilities={"citation_format": 0.72, "polish": 0.2, "unclear": 0.08},
    )


def test_serialization_round_trips() -> None:
    original = _sample()
    restored = decisions_from_json(decisions_to_json(original))
    assert restored is not None
    assert restored.intent == original.intent
    assert restored.needs_clarify is True
    assert restored.path_deep is False
    assert restored.confidence == original.confidence
    assert restored.source == original.source
    assert dict(restored.probabilities) == dict(original.probabilities)


def test_a_restored_decision_is_marked_as_coming_from_the_cache() -> None:
    """Otherwise a cache hit is indistinguishable from a fresh call in the trace,
    and the latency number stops meaning anything."""
    restored = decisions_from_json(decisions_to_json(_sample()))
    assert restored is not None
    assert restored.from_cache is True
    assert _sample().from_cache is False


def test_serialization_is_key_order_stable() -> None:
    assert decisions_to_json(_sample()) == decisions_to_json(_sample())


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        "{}",  # no source
        '{"source": "telepathy", "intent": "polish"}',  # unknown backend name
        '{"source": "jev", "intent": "polish", "confidence": "high"}',  # bad float
        '{"source": "jev", "intent": "polish", "probabilities": 5}',  # bad mapping
        '{"source": "jev", "intent": "polish"',  # truncated
    ],
)
def test_an_untrustworthy_entry_is_a_miss_not_an_error(text: str) -> None:
    """A cache that can raise turns an optimisation into an outage. Every one of
    these is a stale or corrupt entry, and for all of them the right action is
    the same: decide again."""
    assert decisions_from_json(text) is None


def test_an_out_of_vocabulary_intent_in_a_cached_entry_becomes_unclear() -> None:
    """The entry may predate a rename. Serving the old label would send the graph
    down a branch that no longer exists."""
    restored = decisions_from_json('{"source": "jev", "intent": "summarize", "confidence": 0.9}')
    assert restored is not None
    assert restored.intent == "unclear"


def test_a_missing_boolean_in_a_cached_entry_takes_the_safe_side() -> None:
    restored = decisions_from_json('{"source": "small_llm", "intent": "polish"}')
    assert restored is not None
    assert restored.needs_clarify is True
    assert restored.path_deep is False


def test_serialized_form_is_valid_json_with_the_expected_fields() -> None:
    payload = json.loads(decisions_to_json(_sample()))
    assert set(payload) == {
        "intent",
        "needs_clarify",
        "path_deep",
        "confidence",
        "source",
        "probabilities",
        "from_cache",
    }
