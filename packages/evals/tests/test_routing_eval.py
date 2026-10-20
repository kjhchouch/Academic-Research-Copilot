"""Offline tests for the routing eval harness.

Everything here is deterministic and free: no key, no network, no balance. The
metric math is checked against a hand-computed toy set (so a bug in the
arithmetic is caught here, not discovered as a suspicious headline number), and
the driver is checked by pointing a scripted fake router at the real
routing.jsonl. The live run against a real backend is a separate, deliberate act
-- see `evals/__main__.py` -- and is not exercised here.
"""

from __future__ import annotations

import pytest
from evals.routing import BoundaryScore, Row, SubScore, load_rows, run_eval, score
from paper_agent.routing import Decisions, RouteInput

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _dec(
    intent: str = "unclear",
    *,
    needs_clarify: bool = False,
    path_deep: bool = False,
    source: str = "small_llm",
    confidence: float = 1.0,
) -> Decisions:
    return Decisions(
        intent=intent,  # type: ignore[arg-type]
        needs_clarify=needs_clarify,
        path_deep=path_deep,
        confidence=confidence,
        source=source,  # type: ignore[arg-type]
    )


def _row(query: str, kind: str, expect: str | None = None, **kw: object) -> Row:
    return Row(query=query, ui_context={}, kind=kind, expect=expect, **kw)  # type: ignore[arg-type]


class _PerfectRouter:
    """Returns exactly what each row expects, keyed by query.

    Stands in for a flawless router so the top-line metrics can be asserted at
    their ceiling. Reads the expected labels off the loaded rows rather than
    hardcoding them, so it stays perfect as the dataset grows toward the D7
    target of 100.
    """

    def __init__(self, rows: list[Row], *, source: str = "small_llm") -> None:
        self._by_query = {r.query: r for r in rows}
        self._source = source

    async def decide(self, request: RouteInput) -> Decisions:
        row = self._by_query[request.query]
        return Decisions(
            intent=row.expect or "unclear",
            needs_clarify=bool(row.expect_clarify),
            path_deep=bool(row.expect_deep),
            confidence=1.0,
            source=self._source,  # type: ignore[arg-type]
        )


class _RaisingRouter:
    """Simulates a backend that blows up on every row."""

    async def decide(self, request: RouteInput) -> Decisions:
        raise RuntimeError("backend down")


class _SourceByQueryRouter:
    """Perfect answers, but attributes each row to a caller-chosen source.

    Exists to prove the honesty guardrail: the report must show *which* backend
    produced the number, so a mixed Jev/fallback run cannot be described as a
    pure-Jev run.
    """

    def __init__(self, rows: list[Row], sources: dict[str, str]) -> None:
        self._by_query = {r.query: r for r in rows}
        self._sources = sources

    async def decide(self, request: RouteInput) -> Decisions:
        row = self._by_query[request.query]
        return Decisions(
            intent=row.expect or "unclear",
            needs_clarify=bool(row.expect_clarify),
            path_deep=bool(row.expect_deep),
            confidence=1.0,
            source=self._sources[request.query],  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# The confusion matrix, hand-computed
# --------------------------------------------------------------------------- #


def test_score_matches_a_hand_computed_confusion_matrix() -> None:
    """Four rows, every number derived by hand in the comments.

    truth/pred:  lit->lit (hit), polish->unclear (miss), unclear->unclear (hit),
                 unclear(boundary)->polish (miss + leak).
    """
    rows = [
        _row("q1", "normal", "literature_search"),
        _row("q2", "normal", "polish"),
        _row("q3", "negative", "unclear"),
        _row("q4", "injection", "unclear"),
    ]
    preds = [_dec("literature_search"), _dec("unclear"), _dec("unclear"), _dec("polish")]

    report = score(rows, preds)

    # 2 of 4 scored rows correct.
    assert report.n_scored == 4
    assert report.n_errors == 0
    assert report.accuracy == pytest.approx(0.5)

    lit = report.per_intent["literature_search"]
    assert (lit.support, lit.predicted, lit.tp, lit.fp, lit.fn) == (1, 1, 1, 0, 0)
    assert lit.precision == pytest.approx(1.0)
    assert lit.recall == pytest.approx(1.0)
    assert lit.f1 == pytest.approx(1.0)
    assert lit.fpr == pytest.approx(0.0)  # fp 0 / (0 + tn 3)

    pol = report.per_intent["polish"]
    assert (pol.support, pol.predicted, pol.tp, pol.fp, pol.fn) == (1, 1, 0, 1, 1)
    assert pol.precision == pytest.approx(0.0)
    assert pol.recall == pytest.approx(0.0)
    assert pol.f1 == pytest.approx(0.0)
    assert pol.fpr == pytest.approx(1 / 3)  # fp 1 / (1 + tn 2)

    unc = report.per_intent["unclear"]
    # q3 is the hit; q2 (truth polish) was also predicted unclear -> a false
    # positive; q4 (truth unclear) was predicted polish -> a false negative.
    assert (unc.support, unc.predicted, unc.tp, unc.fp, unc.fn) == (2, 2, 1, 1, 1)
    assert unc.precision == pytest.approx(0.5)
    assert unc.recall == pytest.approx(0.5)
    assert unc.f1 == pytest.approx(0.5)
    assert unc.fpr == pytest.approx(0.5)  # fp 1 / (1 + tn 1)

    # Zero-support intents stay in the table but are excluded from the macro
    # averages, so the mean is over {literature_search, polish, unclear} only.
    assert report.per_intent["outline"].support == 0
    assert report.macro_f1 == pytest.approx((1.0 + 0.0 + 0.5) / 3)
    assert report.macro_fpr == pytest.approx((0.0 + 1 / 3 + 0.5) / 3)

    # q4 is an injection row that escaped into a real skill.
    assert report.boundary.n == 2
    assert report.boundary.leaked == 1
    assert report.boundary.leak_rate == pytest.approx(0.5)


def test_score_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="parallel"):
        score([_row("q", "normal", "polish")], [])


# --------------------------------------------------------------------------- #
# Sub-score semantics
# --------------------------------------------------------------------------- #


def test_subscore_rate_is_none_when_nothing_was_measured() -> None:
    """A dataset that grades no clarify rows must report None, not 0.0 -- '0%
    correct' and 'not measured' have to look different."""
    assert SubScore(n=0, correct=0).rate is None
    assert BoundaryScore(n=0, leaked=0).leak_rate is None
    assert SubScore(n=2, correct=1).rate == pytest.approx(0.5)


def test_rows_without_a_label_are_excluded_from_its_subscore() -> None:
    """A row that carries no `expect_deep` must not dilute the path score."""
    rows = [
        _row("deep", "normal", "draft_section", expect_deep=True),
        _row("nolabel", "normal", "polish"),
    ]
    preds = [_dec("draft_section", path_deep=True), _dec("polish", path_deep=True)]
    report = score(rows, preds)
    # Only the first row grades path_deep, and it is correct.
    assert report.path_deep.n == 1
    assert report.path_deep.correct == 1
    assert report.path_deep.rate == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# The driver, against the real dataset
# --------------------------------------------------------------------------- #


async def test_perfect_router_scores_at_the_ceiling_on_real_data() -> None:
    rows = load_rows()
    expected_scored = sum(1 for r in rows if r.expect is not None)
    expected_boundary = sum(1 for r in rows if r.kind in {"negative", "injection"})

    report = await run_eval(_PerfectRouter(rows), rows)

    assert report.n_rows == len(rows)
    assert report.n_scored == expected_scored
    assert report.n_errors == 0
    assert report.accuracy == pytest.approx(1.0)
    assert report.macro_f1 == pytest.approx(1.0)
    assert report.boundary.n == expected_boundary
    assert report.boundary.leak_rate == pytest.approx(0.0)
    assert report.clarify.rate == pytest.approx(1.0)
    assert report.path_deep.rate == pytest.approx(1.0)
    # Every row was answered by the fallback source, and the report says so.
    assert report.by_source == {"small_llm": len(rows)}


async def test_by_source_attributes_a_mixed_run() -> None:
    """The number must carry its provenance: a run split across two backends
    reports both counts, so it cannot be relabelled as a single-backend result."""
    rows = load_rows()
    sources = {r.query: ("jev" if i % 2 else "small_llm") for i, r in enumerate(rows)}
    report = await run_eval(_SourceByQueryRouter(rows, sources), rows)
    assert report.by_source["jev"] == sum(1 for s in sources.values() if s == "jev")
    assert report.by_source["small_llm"] == sum(1 for s in sources.values() if s == "small_llm")
    assert sum(report.by_source.values()) == len(rows)


async def test_a_raising_router_is_survived_and_scored_as_all_misses() -> None:
    """One backend blowing up must not abort the eval, and its rows must count as
    misses rather than vanish from the denominator."""
    rows = load_rows()
    expected_scored = sum(1 for r in rows if r.expect is not None)
    expected_boundary = sum(1 for r in rows if r.kind in {"negative", "injection"})

    report = await run_eval(_RaisingRouter(), rows)

    assert report.n_errors == len(rows)
    assert report.n_scored == expected_scored
    assert report.accuracy == pytest.approx(0.0)
    # An errored boundary row failed to refuse, so it counts as leaked.
    assert report.boundary.leaked == expected_boundary
    assert report.boundary.leak_rate == pytest.approx(1.0)
    assert report.by_source == {}
