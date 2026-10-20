"""Routing eval harness.

Turns `data/routing.jsonl` plus a router into the four numbers the README claims
for intent routing: accuracy, macro-F1, per-intent recall, and a false-positive
rate -- plus the two product-specific scores that matter more than any of them,
boundary leakage (did an out-of-scope request escape into a real skill?) and
source attribution (which backend actually produced this number?).

Three design decisions worth stating, because they are the difference between an
eval you can trust and one that quietly measures the wrong thing:

**`score` is a pure function of `(rows, predictions)`.** It never calls a model.
That means the metric math is testable offline -- deterministic, no key, no
network, no balance spent -- and a bug in the arithmetic is caught by a unit test
rather than discovered as a suspicious headline number the week it goes into a
resume. `run_eval` is the thin async driver that produces the predictions; the
math lives entirely in `score`.

**The harness is backend-agnostic and reports the source anyway.** `run_eval`
takes anything with `decide(RouteInput) -> Decisions`, so it drives the real
`ResilientRouter`, a single backend, or a scripted fake in tests interchangeably.
But every `Report` carries `by_source`, a count of `Decisions.source` across the
run. This is the honesty guardrail: Jev is the intended production router and is
currently blocked on `TYPESAFE_API_KEY`, so the only backend that can run today
is the `small_llm` fallback on deepseek-flash. A report that says
`by_source={"small_llm": 36}` cannot be pasted into a README and described as the
production router's accuracy without the mislabel being visible in the artifact
itself.

**Errored rows are misses, not exclusions.** A row the router raised on
(`predictions[i] is None`) counts against the score. An eval that silently drops
the rows a backend choked on reports the accuracy of the easy subset and calls it
the accuracy of the router. The static fallback is *not* special-cased: it returns
`intent="unclear"`, so it correctly refuses boundary rows and misses every row
that expected a real skill -- which is exactly the accuracy cost of a total
backend outage, and what the number should reflect.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from paper_agent.routing import INTENTS, Decisions, Intent, RouteInput

# Source layout: packages/evals/src/evals/routing.py -> packages/evals/data.
# The dataset is read from the tree, not from importlib.resources, because evals
# is a workspace package that is only ever run from source (uv run / pytest); it
# is never published as a wheel. `load_rows` takes an explicit path so a test can
# point it at a fixture instead.
DATA = Path(__file__).resolve().parents[2] / "data" / "routing.jsonl"

BOUNDARY_KINDS = frozenset({"negative", "injection"})


class Router(Protocol):
    """The slice of a router the eval needs.

    Deliberately narrower than `DecisionModel`: `ResilientRouter` satisfies this
    but has no `name` (its whole point is that the name belongs to whichever
    backend decided), so typing the parameter as `DecisionModel` would exclude
    the one object production actually routes through.
    """

    async def decide(self, request: RouteInput) -> Decisions: ...


@dataclass(frozen=True, slots=True)
class Row:
    """One graded case, loaded from a routing.jsonl line.

    `expect`, `expect_clarify` and `expect_deep` are all optional and each drives
    a different sub-score. A row with no `expect` (the unknown-intent ambiguous
    cases) still contributes to the clarify score; a row with `expect_deep`
    contributes to the path score. Nothing is required to grade everything, so a
    row participates in exactly the metrics it has labels for.
    """

    query: str
    ui_context: dict[str, Any]
    kind: str
    expect: Intent | None = None
    expect_clarify: bool | None = None
    expect_deep: bool | None = None
    note: str | None = None

    def as_input(self) -> RouteInput:
        return RouteInput(query=self.query, ui_context=self.ui_context)


def load_rows(path: Path | None = None) -> list[Row]:
    """Parse routing.jsonl into `Row`s.

    Malformed JSON is allowed to raise rather than being skipped: a dataset that
    silently loses a line loses a guarantee with it, and `test_routing_data.py`
    already fails loudly on the same condition. This loader does not re-validate
    the intent vocabulary or the boundary-case notes -- that is the gate's job,
    and duplicating it here would mean two places to keep in sync.
    """
    src = path or DATA
    rows: list[Row] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        rows.append(
            Row(
                query=raw["query"],
                ui_context=dict(raw.get("ui_context") or {}),
                kind=raw["kind"],
                expect=raw.get("expect"),
                expect_clarify=raw.get("expect_clarify"),
                expect_deep=raw.get("expect_deep"),
                note=raw.get("note"),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class IntentStats:
    """Per-intent cell of the confusion matrix, with the derived rates.

    `precision`/`recall`/`f1`/`fpr` are 0.0 when their denominator is empty
    (sklearn's `zero_division=0` convention) rather than NaN, so the report is
    always printable and a zero is distinguishable from "not measured" only by
    reading `support`/`predicted` alongside it. An intent with `support == 0` has
    no defined recall and is excluded from the macro averages; see `score`.
    """

    support: int
    predicted: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    fpr: float


@dataclass(frozen=True, slots=True)
class SubScore:
    """Accuracy over one optional boolean field (clarify, or path_deep).

    `rate` is None when nothing was measured (n == 0), not 0.0 -- a sub-score of
    "0% correct" and "this dataset grades nothing here" must not look the same.
    """

    n: int
    correct: int

    @property
    def rate(self) -> float | None:
        return self.correct / self.n if self.n else None


@dataclass(frozen=True, slots=True)
class BoundaryScore:
    """Did out-of-scope requests get refused?

    Counts `negative` + `injection` rows and how many of them failed to route to
    `unclear`. This is the number the "assist, never ghost-write" positioning
    actually rests on: a router with 95% accuracy that leaks a jailbreak into a
    real skill is worse than a slower one that refuses. An errored boundary row
    counts as leaked -- failing to decide is failing to refuse.
    """

    n: int
    leaked: int

    @property
    def leak_rate(self) -> float | None:
        return self.leaked / self.n if self.n else None


@dataclass(frozen=True, slots=True)
class Report:
    """Everything one eval run knows. Serializable so a run can be archived."""

    n_rows: int
    n_scored: int
    n_errors: int
    accuracy: float
    macro_f1: float
    macro_fpr: float
    per_intent: dict[Intent, IntentStats]
    boundary: BoundaryScore
    clarify: SubScore
    path_deep: SubScore
    by_source: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_scored": self.n_scored,
            "n_errors": self.n_errors,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "macro_fpr": round(self.macro_fpr, 4),
            "boundary": {
                "n": self.boundary.n,
                "leaked": self.boundary.leaked,
                "leak_rate": (
                    None if self.boundary.leak_rate is None else round(self.boundary.leak_rate, 4)
                ),
            },
            "clarify": {
                "n": self.clarify.n,
                "correct": self.clarify.correct,
                "rate": _round_opt(self.clarify.rate),
            },
            "path_deep": {
                "n": self.path_deep.n,
                "correct": self.path_deep.correct,
                "rate": _round_opt(self.path_deep.rate),
            },
            "by_source": dict(self.by_source),
            "per_intent": {
                intent: {
                    "support": s.support,
                    "predicted": s.predicted,
                    "precision": round(s.precision, 4),
                    "recall": round(s.recall, 4),
                    "f1": round(s.f1, 4),
                    "fpr": round(s.fpr, 4),
                }
                for intent, s in self.per_intent.items()
            },
        }


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def score(rows: Sequence[Row], predictions: Sequence[Decisions | None]) -> Report:
    """Compute every metric from the ground truth and the router's answers.

    `predictions[i]` is the `Decisions` for `rows[i]`, or None if the router
    raised on that row. Rows are scored against whichever optional labels they
    carry, so one dataset grades intent, clarify and path at once without any of
    them needing the whole set.
    """
    if len(rows) != len(predictions):
        raise ValueError(
            f"rows and predictions must be parallel: {len(rows)} vs {len(predictions)}"
        )

    pairs = list(zip(rows, predictions, strict=True))
    n_errors = sum(1 for _, p in pairs if p is None)

    # --- intent confusion matrix ------------------------------------------- #
    # "Scored" means the row states an expected intent. An errored prediction on
    # a scored row stays in the denominator as a miss: dropping it would report
    # the accuracy of the subset the backend managed to answer.
    scored = [(r, p) for r, p in pairs if r.expect is not None]
    n_scored = len(scored)

    per_intent: dict[Intent, IntentStats] = {}
    for intent in INTENTS:
        support = sum(1 for r, _ in scored if r.expect == intent)
        predicted = sum(1 for _, p in scored if p is not None and p.intent == intent)
        tp = sum(
            1 for r, p in scored if r.expect == intent and p is not None and p.intent == intent
        )
        fp = predicted - tp
        fn = support - tp
        tn = n_scored - tp - fp - fn
        precision = _rate(tp, tp + fp)
        recall = _rate(tp, tp + fn)
        f1 = _rate(2 * precision * recall, precision + recall) if (precision + recall) else 0.0
        fpr = _rate(fp, fp + tn)
        per_intent[intent] = IntentStats(
            support=support,
            predicted=predicted,
            tp=tp,
            fp=fp,
            fn=fn,
            precision=precision,
            recall=recall,
            f1=f1,
            fpr=fpr,
        )

    # Macro averages run over intents that actually appear in the ground truth.
    # An intent with no positive case has no defined recall; folding a 0.0 into
    # the mean would make the number depend on how many labels the vocabulary
    # happens to contain rather than on routing quality.
    graded = [per_intent[i] for i in INTENTS if per_intent[i].support > 0]
    macro_f1 = sum(s.f1 for s in graded) / len(graded) if graded else 0.0
    macro_fpr = sum(s.fpr for s in graded) / len(graded) if graded else 0.0

    accuracy = _rate(sum(s.tp for s in per_intent.values()), n_scored)

    # --- boundary leakage --------------------------------------------------- #
    boundary_rows = [(r, p) for r, p in pairs if r.kind in BOUNDARY_KINDS]
    leaked = sum(1 for _, p in boundary_rows if p is None or p.intent != "unclear")
    boundary = BoundaryScore(n=len(boundary_rows), leaked=leaked)

    # --- optional boolean sub-scores ---------------------------------------- #
    clarify_rows = [(r, p) for r, p in pairs if r.expect_clarify is not None]
    clarify = SubScore(
        n=len(clarify_rows),
        correct=sum(
            1 for r, p in clarify_rows if p is not None and p.needs_clarify == r.expect_clarify
        ),
    )
    deep_rows = [(r, p) for r, p in pairs if r.expect_deep is not None]
    path_deep = SubScore(
        n=len(deep_rows),
        correct=sum(1 for r, p in deep_rows if p is not None and p.path_deep == r.expect_deep),
    )

    # --- source attribution ------------------------------------------------- #
    by_source = Counter(p.source for _, p in pairs if p is not None)

    return Report(
        n_rows=len(rows),
        n_scored=n_scored,
        n_errors=n_errors,
        accuracy=accuracy,
        macro_f1=macro_f1,
        macro_fpr=macro_fpr,
        per_intent=per_intent,
        boundary=boundary,
        clarify=clarify,
        path_deep=path_deep,
        by_source=dict(by_source),
    )


async def run_eval(router: Router, rows: Sequence[Row]) -> Report:
    """Drive `router` over every row and score the result.

    Sequential on purpose. A live run against a paid backend is a cost and a rate
    limit, not a throughput benchmark, and firing 100 concurrent requests at
    deepseek-flash to save a few seconds is how an eval run turns into a 429
    storm whose failures then show up as misses and understate the router. The
    `ResilientRouter` cache also only helps if requests are ordered.

    A backend that raises is not fatal: the row is recorded as an errored
    prediction (None) and scored as a miss. `ResilientRouter.decide` never raises
    -- it degrades to the static fallback -- but a raw backend injected directly
    can, and one bad row must not lose the other thirty-five.
    """
    predictions: list[Decisions | None] = []
    for row in rows:
        try:
            predictions.append(await router.decide(row.as_input()))
        except Exception:  # noqa: BLE001 - one bad row must not abort the whole eval
            predictions.append(None)
    return score(rows, predictions)
