"""Intent routing.

Week 1 implements the model-backed tiers of the cascade described in the README,
plus the deterministic boundary half of L0:

    L0  rules / UI signals        guard.py (integrity boundary only)
    L2  Jev, primary              jev.py
    L1  embedding fallback        (not yet)
    L3  small chat model          small_llm.py

The numbers name cost tiers, the arrows in the README name execution order, and
the two are not the same sequence: Jev is tried first because it is the one built
for structured decisions, the embedding tier is *cheaper* but only a degradation
because nearest-neighbour over labelled examples is a cruder instrument, and the
chat model is the last resort.

`decision_model.py` is the abstraction and the question set; `resilience.py`
composes the backends behind a timeout, a circuit breaker and a cache. A caller
that wants routing should build a `ResilientRouter` via `make_router` and call
`decide`, and should not import a backend directly -- the whole point of the
cascade is that no call site knows or cares which tier answered.

L0 is split, and only one half is here. `guard.py` implements the *integrity
boundary* -- the deterministic rules that turn "降重" or "写整篇论文" into a hard
`refuse` regardless of what any model said. The rest of L0 (using `ui_context`
signals to infer intent, e.g. a selection implies `polish`) is not built, because
the model tiers already read `ui_context` and a rules version would duplicate
their judgement. The boundary half is the one worth doing first and separately:
it is deterministic, free, and it has a property the model tiers cannot offer --
the product boundary (no ghost-writing, no plagiarism-ratio reduction) becomes
something that is *enforced* rather than *predicted*, which is the right shape for
a guarantee. `guard_from` is where that enforcement lands, and the graph reads its
`refuse` / `clarify` / `proceed` verdict as a first-class exit (see
`graph/state.py` and `graph/builder.py`).
"""

from __future__ import annotations

from paper_agent.routing.decision_model import (
    INTENTS,
    DecisionModel,
    Decisions,
    DecisionUnavailable,
    Intent,
    RouteInput,
    RouteSource,
    coerce_intent,
    decisions_from_json,
    decisions_to_json,
    noul_to_bool,
    static_fallback,
)
from paper_agent.routing.guard import (
    CLARIFY_MESSAGE,
    REFUSE_MESSAGE,
    BoundaryFamily,
    Guard,
    boundary_violation,
    guard_from,
)
from paper_agent.routing.jev import JEV_TIMEOUT_SECONDS, JevBackend, make_jev_backend
from paper_agent.routing.resilience import (
    DEFAULT_CACHE_TTL_SECONDS,
    CircuitBreaker,
    ResilientRouter,
    decision_key,
    make_router,
)
from paper_agent.routing.small_llm import SMALL_LLM_TIMEOUT_SECONDS, SmallLLMBackend

__all__ = [
    "CLARIFY_MESSAGE",
    "DEFAULT_CACHE_TTL_SECONDS",
    "INTENTS",
    "JEV_TIMEOUT_SECONDS",
    "REFUSE_MESSAGE",
    "SMALL_LLM_TIMEOUT_SECONDS",
    "BoundaryFamily",
    "CircuitBreaker",
    "DecisionModel",
    "DecisionUnavailable",
    "Decisions",
    "Guard",
    "Intent",
    "JevBackend",
    "ResilientRouter",
    "RouteInput",
    "RouteSource",
    "SmallLLMBackend",
    "boundary_violation",
    "coerce_intent",
    "decision_key",
    "decisions_from_json",
    "decisions_to_json",
    "guard_from",
    "make_jev_backend",
    "make_router",
    "noul_to_bool",
    "static_fallback",
]
