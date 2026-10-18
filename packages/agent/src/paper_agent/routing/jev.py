"""Jev (TypeSafe System One) as a routing backend.

Jev answers structured questions -- a choice among labelled alternatives, a
yes/no with a probability -- in one call, which is exactly the shape of a
routing decision. The alternative is asking a chat model for JSON and parsing
it, which works and is what `small_llm.py` does; the difference is that Jev
returns a probability distribution over the intents rather than a string, so a
low-confidence routing is detectable instead of merely wrong.

Local-first, like `observability.make_tracer`: no API key means no backend, not a
crash. `AsyncTypeSafeClient()` raises `TypeSafeError` at *construction* when no
key is available, so the check has to happen before the client is built -- which
is why `make_jev_backend` exists rather than callers constructing the class.

Cost is not modelled. `paper_agent/pricing.py` prices DeepSeek and raises on an
unknown model rather than reporting $0; Jev's own pricing is not in that table,
so this module logs tokens and leaves the money uncomputed instead of guessing a
rate. Note the API documents output tokens as currently free, so the input count
is the one that matters.
"""

from __future__ import annotations

import logging
from typing import Any

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    SystemOneResponse,
)

from paper_agent.routing.decision_model import (
    INTENT_CRITERIA,
    INTENT_INSTRUCTIONS,
    INTENT_QUESTION,
    NEEDS_CLARIFY_CRITERIA,
    NEEDS_CLARIFY_INSTRUCTIONS,
    NEEDS_CLARIFY_QUESTION,
    PATH_DEEP_CRITERIA,
    PATH_DEEP_INSTRUCTIONS,
    PATH_DEEP_QUESTION,
    Decisions,
    DecisionUnavailable,
    RouteInput,
    RouteSource,
    coerce_intent,
    noul_to_bool,
)

logger = logging.getLogger(__name__)

# The budget for the whole routing step, not for a socket read. Routing happens
# before any user-visible token, so it is paid out of the latency the user is
# waiting on; 800ms is what the Week 1 plan allows before the fast path stops
# being fast.
JEV_TIMEOUT_SECONDS = 0.8

# max_retries=0 is load-bearing, not a preference. The SDK defaults are
# max_retries=2, api_timeout_error=True and backoff_initial=0.5, so a call that
# hits the 800ms timeout is retried twice with backoff between: roughly
# 0.8 + 0.45 + 0.8 + 0.9 + 0.8 = 3.7s. RetryPolicy's own `timeout=30.0` total
# budget does not help, because 3.7s is well inside it. Retrying a call whose
# entire purpose is to be fast defeats the timeout; when Jev is slow the correct
# response is to answer from the fallback, not to wait.
NO_RETRY = RetryPolicy(max_retries=0)

# Built once. The question set is fixed for a release of the product -- it is
# tuned against packages/evals/data/routing.jsonl and changing it changes what
# the accuracy number means -- so there is nothing per-request about it.
QUESTIONS: dict[str, Any] = {
    INTENT_QUESTION: Choice(
        instructions=INTENT_INSTRUCTIONS,
        criteria=dict(INTENT_CRITERIA),
    ),
    PATH_DEEP_QUESTION: Noul(
        instructions=PATH_DEEP_INSTRUCTIONS,
        criteria=PATH_DEEP_CRITERIA,
    ),
    NEEDS_CLARIFY_QUESTION: Noul(
        instructions=NEEDS_CLARIFY_INSTRUCTIONS,
        criteria=NEEDS_CLARIFY_CRITERIA,
    ),
}


def _noul_probability(response: SystemOneResponse, name: str) -> float | None:
    """Read one yes/no answer, or None if it is not there.

    `.nouls` is a `cached_property` that filters `response.answers` by type, not
    a field the server guarantees to populate. Two ways a question can be
    missing: the service dropped it, or a future API returns an answer kind this
    SDK version does not know -- the decoder deliberately skips unknown kinds
    rather than failing the whole response. Both are "no decision", which
    `noul_to_bool` turns into that question's safe default.

    Indexing instead (`response.nouls[name]`) would raise KeyError on either
    case, and a KeyError escaping `decide` would 500 the request over a routing
    detail.
    """
    answer = response.nouls.get(name)
    return None if answer is None else answer.noul


class JevBackend:
    """`DecisionModel` backed by TypeSafe System One.

    `transport` is a passthrough to the SDK's httpx transport and exists for
    tests: it is the seam that lets the whole decode-and-binarize path run with
    no API key and no network. Prefer it over monkeypatching `decide`.
    """

    name: RouteSource = "jev"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "jev",
        timeout: float = JEV_TIMEOUT_SECONDS,
        transport: Any = None,
        base_url: str | None = None,
    ) -> None:
        self._timeout = timeout
        self._client = AsyncTypeSafeClient(
            api_key=api_key,
            model=model,
            timeout=timeout,
            retry=NO_RETRY,
            transport=transport,
            base_url=base_url,
        )

    async def decide(self, request: RouteInput) -> Decisions:
        try:
            response = await self._client.system_one(
                request.as_state(), QUESTIONS, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - a routing outage must not 500 the request
            # Broad on purpose. TypeSafeError covers the documented cases
            # (timeout, connection, auth, rate limit, response validation), but
            # the promise this backend makes is "I either decide or I raise
            # DecisionUnavailable", and an undocumented httpx or pydantic error
            # escaping would break that promise in production only. The type name
            # is logged so the distinction between expected and unexpected is
            # still visible after the fact.
            logger.warning("jev routing failed: %s: %s", type(exc).__name__, exc)
            raise DecisionUnavailable(f"jev: {type(exc).__name__}") from exc

        intent_answer = response.choices.get(INTENT_QUESTION)
        if intent_answer is None:
            # Unlike a missing Noul this is not degradable: the intent is the
            # decision, and every other field is a qualifier on it.
            logger.warning("jev routing returned no %r answer", INTENT_QUESTION)
            raise DecisionUnavailable("jev: no intent answer")

        logger.info(
            "jev routing: model=%s input_tokens=%d output_tokens=%d",
            response.model,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        return Decisions(
            intent=coerce_intent(intent_answer.choice),
            # Opposite defaults, and not by accident: guessing a missing slot
            # produces confidently wrong work, while the deep path is a
            # multi-agent pipeline that should not be spent on a narrow request.
            needs_clarify=noul_to_bool(
                _noul_probability(response, NEEDS_CLARIFY_QUESTION), uncertain=True
            ),
            path_deep=noul_to_bool(
                _noul_probability(response, PATH_DEEP_QUESTION), uncertain=False
            ),
            confidence=intent_answer.confidence,
            source=self.name,
            probabilities=dict(intent_answer.probabilities),
        )

    async def aclose(self) -> None:
        """Release the httpx client. Safe to call twice."""
        await self._client.aclose()


def make_jev_backend(
    *,
    api_key: str | None,
    model: str = "jev",
    timeout: float = JEV_TIMEOUT_SECONDS,
    transport: Any = None,
    base_url: str | None = None,
) -> JevBackend | None:
    """Build a Jev backend, or return None when no key is configured.

    Same contract as `observability.make_tracer`: absence of credentials is a
    supported configuration, not an error, and the app must boot and route
    without it. Callers treat None as "use the fallback backend".
    """
    if not api_key:
        logger.info("jev routing disabled: TYPESAFE_API_KEY not configured")
        return None
    return JevBackend(
        api_key=api_key,
        model=model,
        timeout=timeout,
        transport=transport,
        base_url=base_url,
    )
