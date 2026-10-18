"""The two routing backends.

Jev is exercised through the SDK's `transport` seam, so these tests run the real
request serialization, the real response decoder and the real pydantic validation
-- only the socket is replaced. That is worth more than mocking `decide`, which
would test the mock.

What cannot be tested this way: an actual 800ms timeout firing. `MockTransport`
short-circuits the network layer, so httpx never arms a timer and a handler that
sleeps simply returns late. The timeout is therefore pinned two ways instead --
as configuration (it is the value handed to the client) and as behaviour on the
exception the timeout produces (`TypeSafeAPITimeoutError`, verified below to be
what the SDK raises).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from langchain_core.messages import AIMessage
from paper_agent.routing.decision_model import (
    INTENTS,
    DecisionUnavailable,
    RouteInput,
)
from paper_agent.routing.jev import (
    JEV_TIMEOUT_SECONDS,
    NO_RETRY,
    QUESTIONS,
    JevBackend,
    make_jev_backend,
)
from paper_agent.routing.small_llm import SYSTEM_PROMPT, SmallLLMBackend
from paper_agent.testing import ScriptedChatModel

# --------------------------------------------------------------------------- #
# Jev helpers
# --------------------------------------------------------------------------- #


def _payload(
    *,
    intent: str = "polish",
    confidence: float = 0.91,
    probabilities: dict[str, float] | None = None,
    path_deep: float | None = 0.02,
    needs_clarify: float | None = 0.11,
    model: str = "jev",
) -> dict[str, Any]:
    """A wire-format System One response.

    Built as raw JSON rather than from SDK objects on purpose: this is the shape
    the service sends, and constructing it through the SDK's own types would let a
    change in those types keep the test passing while the real payload broke.
    """
    answers: dict[str, Any] = {
        "intent": {
            "type": "choice",
            "choice": intent,
            "confidence": confidence,
            "probabilities": probabilities or {intent: confidence},
        }
    }
    if path_deep is not None:
        answers["path_deep"] = {"type": "noul", "noul": path_deep}
    if needs_clarify is not None:
        answers["needs_clarify"] = {"type": "noul", "noul": needs_clarify}
    return {
        "model": model,
        "usage": {"input_tokens": 120, "output_tokens": 12},
        "answers": answers,
    }


def _backend(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    timeout: float = JEV_TIMEOUT_SECONDS,
) -> JevBackend:
    return JevBackend(api_key="test-key", timeout=timeout, transport=httpx2.MockTransport(handler))


def _json_backend(
    payload: dict[str, Any], *, seen: list[httpx2.Request] | None = None
) -> JevBackend:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return httpx2.Response(200, json=payload)

    return _backend(handler)


REQUEST = RouteInput(query="把这段改好一点", ui_context={"active_panel": "editor"})


# --------------------------------------------------------------------------- #
# Jev: the happy path
# --------------------------------------------------------------------------- #


async def test_jev_returns_a_full_decision() -> None:
    seen: list[httpx2.Request] = []
    backend = _json_backend(
        _payload(
            intent="citation_format",
            confidence=0.72,
            probabilities={"citation_format": 0.72, "polish": 0.2, "unclear": 0.08},
        ),
        seen=seen,
    )
    decisions = await backend.decide(REQUEST)

    assert decisions.intent == "citation_format"
    assert decisions.needs_clarify is False
    assert decisions.path_deep is False
    assert decisions.confidence == 0.72
    assert decisions.source == "jev"
    assert decisions.from_cache is False
    assert decisions.probabilities["polish"] == 0.2
    await backend.aclose()


async def test_jev_sends_the_query_and_ui_context_as_state() -> None:
    """ui_context is the stronger signal, so it has to actually be on the wire."""
    seen: list[httpx2.Request] = []
    backend = _json_backend(_payload(), seen=seen)
    await backend.decide(REQUEST)

    body = json.loads(seen[0].content)
    assert body["state"] == {"query": "把这段改好一点", "ui_context": {"active_panel": "editor"}}
    assert body["model"] == "jev"
    assert set(body["questions"]) == {"intent", "path_deep", "needs_clarify"}
    await backend.aclose()


@pytest.mark.parametrize(
    ("path_deep", "needs_clarify", "expected_deep", "expected_clarify"),
    [
        (0.97, 0.93, True, True),
        (0.03, 0.07, False, False),
        (0.97, 0.03, True, False),
        (0.03, 0.93, False, True),
        # Both undecided: each question falls to its own safe side, which is why
        # the two columns differ on the same input.
        (0.5, 0.5, False, True),
    ],
)
async def test_jev_binarizes_nouls(
    path_deep: float,
    needs_clarify: float,
    expected_deep: bool,
    expected_clarify: bool,
) -> None:
    backend = _json_backend(_payload(path_deep=path_deep, needs_clarify=needs_clarify))
    decisions = await backend.decide(REQUEST)
    assert decisions.path_deep is expected_deep
    assert decisions.needs_clarify is expected_clarify
    await backend.aclose()


# --------------------------------------------------------------------------- #
# Jev: degraded responses
# --------------------------------------------------------------------------- #


async def test_a_missing_noul_takes_the_safe_default_rather_than_failing() -> None:
    """The intent is the decision; the Nouls are qualifiers on it. A service that
    answers one question and not the other should still route."""
    backend = _json_backend(_payload(path_deep=None, needs_clarify=None))
    decisions = await backend.decide(REQUEST)
    assert decisions.intent == "polish"
    assert decisions.needs_clarify is True
    assert decisions.path_deep is False
    await backend.aclose()


async def test_an_answer_kind_this_sdk_does_not_model_is_skipped_not_fatal() -> None:
    """The decoder drops unknown answer types for forward compatibility, so a
    server-side addition surfaces here as a missing answer, not as a crash."""
    payload = _payload(needs_clarify=None)
    payload["answers"]["needs_clarify"] = {"type": "ranking", "ranked": ["a", "b"]}
    backend = _json_backend(payload)
    decisions = await backend.decide(REQUEST)
    assert decisions.needs_clarify is True  # the safe default
    await backend.aclose()


async def test_a_missing_intent_answer_is_unavailable_not_unclear() -> None:
    """`unclear` is an answer the router can act on. "Jev did not answer" is an
    outage, and reporting it as the former would let a broken primary look like a
    conversation full of ambiguous users -- and would never trip the breaker."""
    payload = _payload()
    del payload["answers"]["intent"]
    backend = _json_backend(payload)
    with pytest.raises(DecisionUnavailable, match="no intent answer"):
        await backend.decide(REQUEST)
    await backend.aclose()


@pytest.mark.parametrize(
    "intent",
    ["literature-search", "summarize", "", "POLISH"],
)
async def test_an_out_of_vocabulary_choice_becomes_unclear(intent: str) -> None:
    backend = _json_backend(_payload(intent=intent))
    decisions = await backend.decide(REQUEST)
    assert decisions.intent == "unclear"
    await backend.aclose()


# --------------------------------------------------------------------------- #
# Jev: outages
# --------------------------------------------------------------------------- #


def _raise(exc: BaseException) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "label"),
    [
        (_raise(httpx2.ReadTimeout("slow")), "timeout"),
        (_raise(httpx2.ConnectError("unreachable")), "connection"),
        (lambda r: httpx2.Response(500, json={"error": "boom"}), "server error"),
        (lambda r: httpx2.Response(429, json={"error": "slow down"}), "rate limit"),
        (lambda r: httpx2.Response(401, json={"error": "bad key"}), "auth"),
        (lambda r: httpx2.Response(200, content=b"not json"), "malformed body"),
    ],
    ids=["timeout", "connection", "server-error", "rate-limit", "auth", "malformed"],
)
async def test_every_outage_becomes_decision_unavailable(handler: Any, label: str) -> None:
    """One exception type for the resilience layer to catch.

    The SDK maps these to six distinct classes (verified against 0.7.5:
    TypeSafeAPITimeoutError, TypeSafeAPIConnectionError, TypeSafeInternalServerError,
    TypeSafeRateLimitError, TypeSafeAuthenticationError,
    TypeSafeAPIResponseValidationError). Leaking that taxonomy upward would make
    `resilience.py` import the Jev SDK, and the breaker would need a case per
    vendor.
    """
    backend = _backend(handler)
    with pytest.raises(DecisionUnavailable):
        await backend.decide(REQUEST)
    await backend.aclose()


async def test_a_failed_call_is_not_retried() -> None:
    """Behavioural pin on `max_retries=0`.

    The SDK defaults to max_retries=2 with backoff_initial=0.5 and
    api_timeout_error=True, so a default-configured 800ms call that times out
    costs about 3.7s -- nearly five times the budget the fast path allows.
    Asserting the constant alone would not catch a client built without it.
    """
    attempts: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        return httpx2.Response(500, json={"error": "boom"})

    backend = _backend(handler)
    with pytest.raises(DecisionUnavailable):
        await backend.decide(REQUEST)
    assert len(attempts) == 1
    await backend.aclose()


def test_the_no_retry_policy_is_the_one_that_disables_retrying() -> None:
    assert NO_RETRY.max_retries == 0


def test_the_timeout_budget_is_what_the_plan_allows() -> None:
    assert JEV_TIMEOUT_SECONDS == 0.8


async def test_the_configured_timeout_reaches_the_client() -> None:
    """Pins that the 800ms budget survives construction.

    One attribute into the SDK's own config object, which is brittle across SDK
    versions -- but the failure this guards is silent and expensive: drop
    `timeout=` from the ctor call and routing inherits the SDK default, so a slow
    Jev turns a fast-path request into a 30s hang. If an upgrade breaks this, fix
    it by re-reading where the SDK stores the value, not by deleting the assert.
    """
    backend = JevBackend(api_key="test-key", timeout=0.25)
    try:
        assert backend._client._config.timeout == 0.25  # noqa: SLF001
    finally:
        await backend.aclose()


# --------------------------------------------------------------------------- #
# Jev: configuration
# --------------------------------------------------------------------------- #


def test_no_key_means_no_backend() -> None:
    """`AsyncTypeSafeClient()` raises TypeSafeError at construction without a key,
    so the check has to happen first. Same local-first contract as
    `observability.make_tracer`: missing credentials are a configuration, not an
    error."""
    assert make_jev_backend(api_key=None) is None
    assert make_jev_backend(api_key="") is None
    assert isinstance(make_jev_backend(api_key="test-key"), JevBackend)


def test_the_question_set_covers_exactly_the_intent_vocabulary() -> None:
    """If the criteria and `INTENTS` drift, the router cannot produce some label
    the eval expects, and the missing one shows up as a recall failure that looks
    like a model problem."""
    choice = QUESTIONS["intent"]
    assert set(choice.criteria) == set(INTENTS)


def test_the_question_set_asks_all_three_questions() -> None:
    assert set(QUESTIONS) == {"intent", "path_deep", "needs_clarify"}
    assert QUESTIONS["intent"].type == "choice"
    assert QUESTIONS["path_deep"].type == "noul"
    assert QUESTIONS["needs_clarify"].type == "noul"


def test_both_nouls_describe_both_outcomes() -> None:
    """A Noul with `instructions` but no described outcomes still validates, and
    then "yes" means whatever the model guessed it meant. Describing both sides is
    what makes the probability comparable across requests."""
    for name in ("path_deep", "needs_clarify"):
        criteria = QUESTIONS[name].criteria
        assert criteria is not None, name
        assert criteria.get("true"), f"{name} does not describe the yes outcome"
        assert criteria.get("false"), f"{name} does not describe the no outcome"


async def test_token_usage_is_logged_for_the_trace(caplog: pytest.LogCaptureFixture) -> None:
    """The D4 acceptance criterion asks for Jev latency and cost to be visible.
    Cost is not computed -- `pricing.py` prices DeepSeek and raises on an unknown
    model rather than reporting $0 -- so tokens are what gets recorded."""
    caplog.set_level(logging.INFO, logger="paper_agent.routing.jev")
    backend = _json_backend(_payload())
    await backend.decide(REQUEST)
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "input_tokens=120" in line
    assert "output_tokens=12" in line
    await backend.aclose()


# --------------------------------------------------------------------------- #
# SmallLLM
# --------------------------------------------------------------------------- #


def _llm(reply: str) -> SmallLLMBackend:
    return SmallLLMBackend(ScriptedChatModel(responses=[AIMessage(content=reply)]))


VALID_REPLY = json.dumps(
    {"intent": "polish", "path_deep": False, "needs_clarify": False, "confidence": 0.83}
)

# Built rather than written out: the fallback's own prompt shows the model a
# pretty-printed object, so this is the shape it is most likely to echo.
MULTILINE_REPLY = json.dumps(
    {"intent": "polish", "path_deep": False, "needs_clarify": False, "confidence": 0.8},
    indent=2,
    ensure_ascii=False,
)


async def test_small_llm_returns_a_full_decision() -> None:
    decisions = await _llm(VALID_REPLY).decide(REQUEST)
    assert decisions.intent == "polish"
    assert decisions.path_deep is False
    assert decisions.needs_clarify is False
    assert decisions.confidence == 0.83
    assert decisions.source == "small_llm"
    assert decisions.probabilities == {}


@pytest.mark.parametrize(
    "reply",
    [
        VALID_REPLY,
        f"```json\n{VALID_REPLY}\n```",
        f"好的，以下是结果：\n{VALID_REPLY}",
        MULTILINE_REPLY,
        f"some preamble {VALID_REPLY} and a trailing remark",
    ],
    ids=["plain", "fenced", "prefixed", "multiline", "embedded"],
)
async def test_decoration_around_the_json_is_tolerated(reply: str) -> None:
    """Models fence and narrate even when told not to. Rejecting that would make
    the fallback path fail on formatting rather than on judgement."""
    decisions = await _llm(reply).decide(REQUEST)
    assert decisions.intent == "polish"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("FALSE", False),
        ("yes", True),
        ("no", False),
        ("是", True),
        ("否", False),
        (1, True),
        (0, False),
    ],
)
async def test_a_models_idea_of_a_boolean_is_coerced(raw: Any, expected: bool) -> None:
    reply = json.dumps(
        {"intent": "outline", "path_deep": raw, "needs_clarify": raw, "confidence": 0.5}
    )
    decisions = await _llm(reply).decide(REQUEST)
    assert decisions.path_deep is expected
    assert decisions.needs_clarify is expected


@pytest.mark.parametrize(
    ("payload", "expected_clarify", "expected_deep"),
    [
        # Both keys absent.
        ({"intent": "outline", "confidence": 0.5}, True, False),
        # Present but null -- what a model emits when it will not commit.
        (
            {"intent": "outline", "needs_clarify": None, "path_deep": None},
            True,
            False,
        ),
        # Only one missing: the other must still be honoured.
        ({"intent": "outline", "path_deep": True}, True, True),
        ({"intent": "outline", "needs_clarify": False}, False, False),
    ],
    ids=["absent", "null", "only-path-deep", "only-needs-clarify"],
)
async def test_missing_booleans_take_the_same_safe_side_as_jev(
    payload: dict[str, Any], expected_clarify: bool, expected_deep: bool
) -> None:
    """The two backends must degrade identically, or switching between them
    changes product behaviour -- which is the one thing the fallback exists to
    prevent."""
    decisions = await _llm(json.dumps(payload)).decide(REQUEST)
    assert decisions.needs_clarify is expected_clarify
    assert decisions.path_deep is expected_deep


async def test_an_unparseable_boolean_takes_the_safe_side() -> None:
    reply = json.dumps(
        {"intent": "polish", "path_deep": "maybe", "needs_clarify": "不太确定", "confidence": 0.5}
    )
    decisions = await _llm(reply).decide(REQUEST)
    assert decisions.needs_clarify is True
    assert decisions.path_deep is False


async def test_an_out_of_vocabulary_intent_becomes_unclear_and_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both backends end up at `unclear`, but only this one invented a label --
    and that means the prompt or the vocabulary drifted. Silent coercion would
    hide it behind a plausible-looking accuracy number."""
    caplog.set_level(logging.WARNING, logger="paper_agent.routing.small_llm")
    reply = json.dumps({"intent": "summarize", "confidence": 0.9})
    decisions = await _llm(reply).decide(REQUEST)
    assert decisions.intent == "unclear"
    assert "out-of-vocabulary intent" in caplog.text


@pytest.mark.parametrize(
    "reply",
    ["", "I cannot answer that", "{", '{"intent": }', "[1,2,3]", '{"intent": "polish"'],
    ids=["empty", "prose", "lone-brace", "malformed", "array", "truncated"],
)
async def test_an_unusable_reply_is_unavailable(reply: str) -> None:
    with pytest.raises(DecisionUnavailable):
        await _llm(reply).decide(REQUEST)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.5, 0.5), (1.7, 1.0), (-0.3, 0.0), ("high", 0.0), (None, 0.0), ("0.4", 0.4)],
)
async def test_confidence_is_clamped_and_never_raises(raw: Any, expected: float) -> None:
    reply = json.dumps({"intent": "polish", "confidence": raw})
    decisions = await _llm(reply).decide(REQUEST)
    assert decisions.confidence == expected


async def test_a_slow_model_is_bounded() -> None:
    """`wait_for` rather than the model's own timeout: this backend accepts any
    Runnable and cannot assume it honours one."""

    class _Slow:
        async def ainvoke(self, messages: Any) -> AIMessage:
            await asyncio.sleep(1.0)
            return AIMessage(content=VALID_REPLY)

    backend = SmallLLMBackend(_Slow(), timeout=0.05)
    with pytest.raises(DecisionUnavailable, match="timeout"):
        await backend.decide(REQUEST)


async def test_a_model_that_raises_is_unavailable() -> None:
    class _Broken:
        async def ainvoke(self, messages: Any) -> AIMessage:
            raise RuntimeError("upstream 500")

    with pytest.raises(DecisionUnavailable):
        await SmallLLMBackend(_Broken()).decide(REQUEST)


async def test_list_shaped_content_is_flattened() -> None:
    """Some providers return content as a list of blocks rather than a str."""
    message = AIMessage(content=[{"type": "text", "text": VALID_REPLY}])
    decisions = await SmallLLMBackend(ScriptedChatModel(responses=[message])).decide(REQUEST)
    assert decisions.intent == "polish"


def test_the_prompt_names_every_intent() -> None:
    """The criteria are rendered into the prompt at import time. If that
    rendering broke, the model would be choosing from a list it cannot see, and
    every answer would come back out-of-vocabulary."""
    for intent in INTENTS:
        assert intent in SYSTEM_PROMPT


def test_the_prompt_states_the_product_boundary() -> None:
    """The boundary has to be in the fallback's prompt too. It is the backend that
    runs when the primary is down, so a boundary that only the primary knows
    about is a boundary that disappears during an outage."""
    assert "代写" in SYSTEM_PROMPT
    assert "降重" in SYSTEM_PROMPT
