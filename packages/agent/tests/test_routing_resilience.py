"""Circuit breaker, cache, and the cascade that composes the two backends.

The clock is injectable because every interesting transition here is a passage of
time, and a test that sleeps 30 real seconds to watch a breaker recover is a test
nobody runs twice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langchain_core.messages import AIMessage
from paper_agent.routing.decision_model import (
    INTENTS,
    Decisions,
    DecisionUnavailable,
    RouteInput,
    static_fallback,
)
from paper_agent.routing.jev import JevBackend
from paper_agent.routing.resilience import (
    CircuitBreaker,
    ResilientRouter,
    decision_key,
    make_router,
)
from paper_agent.routing.small_llm import SmallLLMBackend
from paper_agent.storage import MemoryCache
from paper_agent.testing import ScriptedChatModel


class FakeClock:
    """A monotonic clock a test can move."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _decisions(source: str = "jev", intent: str = "polish") -> Decisions:
    return Decisions(
        intent=intent,  # type: ignore[arg-type]
        needs_clarify=False,
        path_deep=False,
        confidence=0.9,
        source=source,  # type: ignore[arg-type]
    )


class FakeBackend:
    """A `DecisionModel` that either answers or fails, and counts its calls."""

    def __init__(
        self,
        name: str,
        *,
        decisions: Decisions | None = None,
        fail: bool = False,
        closes: bool = True,
    ) -> None:
        self.name = name
        self.calls = 0
        self.closed = 0
        self._decisions = decisions or _decisions(name)
        self._fail = fail
        if not closes:
            # Deleting the method is how a backend without cleanup is simulated;
            # `getattr(backend, "aclose", None)` has to cope with it.
            self.aclose = None  # type: ignore[assignment]

    async def decide(self, request: RouteInput) -> Decisions:
        self.calls += 1
        if self._fail:
            raise DecisionUnavailable(f"{self.name}: down")
        return self._decisions

    async def aclose(self) -> None:
        self.closed += 1


REQUEST = RouteInput(query="把这段改好一点", ui_context={"active_panel": "editor"})

# packages/agent/tests -> packages/evals/data. Reading the real corpus rather
# than inlining a copy means this test tracks the dataset the D7 accuracy number
# is computed from; a second list of 20 sentences would drift from the first and
# then measure nothing. The coupling is test-only, and the evals package already
# depends on paper-agent, so nothing at runtime reaches across.
_CORPUS = Path(__file__).resolve().parents[2] / "evals" / "data" / "routing.jsonl"


def _eval_rows() -> list[dict[str, Any]]:
    if not _CORPUS.exists():
        pytest.skip(f"routing corpus not found at {_CORPUS}")
    return [
        json.loads(line)
        for line in _CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _CannedJevBackend(JevBackend):
    """A real `JevBackend` whose socket is replaced.

    Subclassing rather than a fresh fake, so the corpus test exercises the actual
    request serialization, response decoding and Noul binarization instead of a
    parallel implementation that could drift from it.
    """

    def __init__(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "model": "jev",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "answers": {
                        "intent": {
                            "type": "choice",
                            "choice": "polish",
                            "confidence": 0.8,
                            "probabilities": {"polish": 0.8},
                        },
                        "path_deep": {"type": "noul", "noul": 0.1},
                        "needs_clarify": {"type": "noul", "noul": 0.9},
                    },
                },
            )

        super().__init__(api_key="test-key", transport=httpx2.MockTransport(handler))


# --------------------------------------------------------------------------- #
# CircuitBreaker
# --------------------------------------------------------------------------- #


def test_a_new_breaker_is_closed(clock: FakeClock) -> None:
    breaker = CircuitBreaker(clock=clock)
    assert breaker.state == "closed"
    assert breaker.allow_request() is True


def test_failures_below_the_threshold_do_not_open(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.allow_request() is True
    assert breaker.failures == 2


def test_the_threshold_is_consecutive_failures(clock: FakeClock) -> None:
    """A success in between resets the count. The breaker is asking "is this
    backend down right now", not "has it ever been unreliable"."""
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.failures == 2


def test_opening_at_the_threshold_blocks_requests(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_a_successful_probe_closes_the_breaker(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock)
    breaker.record_failure()
    assert breaker.state == "open"

    clock.advance(30.0)
    assert breaker.state == "half_open"
    assert breaker.allow_request() is True

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.failures == 0
    assert breaker.allow_request() is True


def test_a_failed_probe_reopens_for_a_full_window(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock)
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.allow_request() is True

    breaker.record_failure()
    assert breaker.state == "open"

    # Not immediately half-open again: the window restarts from the failed probe.
    clock.advance(29.9)
    assert breaker.state == "open"
    clock.advance(0.2)
    assert breaker.state == "half_open"


def test_half_open_admits_exactly_one_probe(clock: FakeClock) -> None:
    """The point of half-open. Admitting every request that arrives at the
    recovery boundary would send a whole burst at a backend that is still
    suspected down, and turn one recovery window into N failures."""
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock)
    breaker.record_failure()
    clock.advance(30.0)

    assert breaker.allow_request() is True
    assert breaker.allow_request() is False
    assert breaker.allow_request() is False


def test_recovery_is_not_early(clock: FakeClock) -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock)
    breaker.record_failure()
    clock.advance(29.999)
    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_a_threshold_below_one_is_rejected(clock: FakeClock) -> None:
    """0 would open on the first failure and make the counter meaningless; a
    negative one would never open at all."""
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreaker(failure_threshold=0, clock=clock)


# --------------------------------------------------------------------------- #
# The cascade
# --------------------------------------------------------------------------- #


async def test_the_primary_answers_when_healthy() -> None:
    primary = FakeBackend("jev")
    fallback = FakeBackend("small_llm")
    router = ResilientRouter(primary=primary, fallback=fallback)

    decisions = await router.decide(REQUEST)
    assert decisions.intent == "polish"
    assert decisions.source == "jev"
    assert decisions.from_cache is False
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_a_failed_primary_hands_off_to_the_fallback() -> None:
    primary = FakeBackend("jev", fail=True)
    fallback = FakeBackend("small_llm")
    router = ResilientRouter(primary=primary, fallback=fallback)

    decisions = await router.decide(REQUEST)
    assert decisions.source == "small_llm"
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_an_open_breaker_stops_calling_the_primary() -> None:
    """The whole reason the breaker exists. Without it every request during an
    outage pays the primary's full timeout before degrading."""
    primary = FakeBackend("jev", fail=True)
    fallback = FakeBackend("small_llm")
    router = ResilientRouter(
        primary=primary,
        fallback=fallback,
        breaker=CircuitBreaker(failure_threshold=2, recovery_timeout=30.0),
    )

    for _ in range(5):
        await router.decide(RouteInput(query=f"q{_}"))

    assert router.breaker.state == "open"
    assert primary.calls == 2  # tried until it opened, then never again
    assert fallback.calls == 5  # every request still got a real answer


async def test_a_recovered_primary_is_probed_and_resumed(clock: FakeClock) -> None:
    primary = FakeBackend("jev", fail=True)
    fallback = FakeBackend("small_llm")
    router = ResilientRouter(
        primary=primary,
        fallback=fallback,
        breaker=CircuitBreaker(failure_threshold=1, recovery_timeout=30.0, clock=clock),
    )

    await router.decide(RouteInput(query="a"))
    assert router.breaker.state == "open"

    clock.advance(30.0)
    primary._fail = False  # the outage is over

    decisions = await router.decide(RouteInput(query="b"))
    assert decisions.source == "jev"
    assert router.breaker.state == "closed"


async def test_total_failure_degrades_to_the_static_decision() -> None:
    router = ResilientRouter(
        primary=FakeBackend("jev", fail=True), fallback=FakeBackend("small_llm", fail=True)
    )
    decisions = await router.decide(REQUEST)
    assert decisions == static_fallback()
    assert decisions.intent == "unclear"
    assert decisions.needs_clarify is True


async def test_a_router_with_no_backends_at_all_still_answers() -> None:
    """Boots, serves, and asks the user to rephrase. A worse product than a
    working router and a better one than a gateway that will not start."""
    decisions = await ResilientRouter().decide(REQUEST)
    assert decisions == static_fallback()


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


async def test_a_second_identical_request_is_served_from_the_cache() -> None:
    primary = FakeBackend("jev")
    cache = MemoryCache()
    router = ResilientRouter(primary=primary, cache=cache)

    first = await router.decide(REQUEST)
    second = await router.decide(REQUEST)

    assert primary.calls == 1
    assert second.intent == first.intent
    assert second.from_cache is True
    assert first.from_cache is False


async def test_a_cache_hit_keeps_the_backend_that_decided() -> None:
    """`from_cache` and `source` answer different questions. Reporting the cache
    as the source would make the eval unable to attribute accuracy to a backend,
    and the trace unable to show that a degraded answer is being replayed."""
    primary = FakeBackend("jev", fail=True)
    fallback = FakeBackend("small_llm")
    router = ResilientRouter(primary=primary, fallback=fallback, cache=MemoryCache())

    await router.decide(REQUEST)
    replayed = await router.decide(REQUEST)
    assert replayed.source == "small_llm"
    assert replayed.from_cache is True


async def test_a_different_request_is_not_a_cache_hit() -> None:
    primary = FakeBackend("jev")
    router = ResilientRouter(primary=primary, cache=MemoryCache())
    await router.decide(REQUEST)
    await router.decide(RouteInput(query="帮我找几篇论文"))
    assert primary.calls == 2


async def test_the_static_fallback_is_never_cached() -> None:
    """It means "every backend failed". Caching it would keep serving "please
    rephrase" for a full TTL after the backends recovered -- an outage that
    outlives its cause."""
    cache = MemoryCache()
    router = ResilientRouter(
        primary=FakeBackend("jev", fail=True),
        fallback=FakeBackend("small_llm", fail=True),
        cache=cache,
    )
    await router.decide(REQUEST)
    assert await cache.get(decision_key(REQUEST)) is None


async def test_the_cache_is_consulted_while_the_breaker_is_open() -> None:
    """A cached answer needs no backend, so an outage should not cost latency on
    requests already decided."""
    cache = MemoryCache()
    key = decision_key(REQUEST)
    await cache.set(key, '{"source":"jev","intent":"outline","confidence":0.9}', 60)

    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()
    assert breaker.state == "open"

    primary = FakeBackend("jev")
    router = ResilientRouter(primary=primary, cache=cache, breaker=breaker)
    decisions = await router.decide(REQUEST)
    assert decisions.intent == "outline"
    assert decisions.from_cache is True
    assert primary.calls == 0


class _BrokenCache:
    """A cache that fails on every operation."""

    async def get(self, key: str) -> str | None:
        raise RuntimeError("redis down")

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        raise RuntimeError("redis down")


async def test_a_broken_cache_degrades_to_deciding_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cache is an optimisation. If it could break the request, adding it
    would have made routing less reliable than not having it."""
    caplog.set_level(logging.WARNING, logger="paper_agent.routing.resilience")
    primary = FakeBackend("jev")
    router = ResilientRouter(primary=primary, cache=_BrokenCache())

    decisions = await router.decide(REQUEST)
    assert decisions.source == "jev"
    assert "route cache read failed" in caplog.text

    await router.decide(RouteInput(query="another"))
    assert "route cache write failed" in caplog.text


async def test_a_corrupt_cache_entry_is_a_miss(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="paper_agent.routing.resilience")
    cache = MemoryCache()
    await cache.set(decision_key(REQUEST), "not json at all", 60)

    primary = FakeBackend("jev")
    router = ResilientRouter(primary=primary, cache=cache)
    decisions = await router.decide(REQUEST)

    assert decisions.from_cache is False
    assert primary.calls == 1
    assert "unparsable route cache entry" in caplog.text


async def test_a_cached_entry_is_overwritten_with_a_good_one() -> None:
    cache = MemoryCache()
    await cache.set(decision_key(REQUEST), "not json at all", 60)
    router = ResilientRouter(primary=FakeBackend("jev"), cache=cache)

    await router.decide(REQUEST)
    second = await router.decide(REQUEST)
    assert second.from_cache is True


async def test_a_router_with_no_cache_still_works() -> None:
    primary = FakeBackend("jev")
    router = ResilientRouter(primary=primary)
    await router.decide(REQUEST)
    await router.decide(REQUEST)
    assert primary.calls == 2


# --------------------------------------------------------------------------- #
# Lifecycle and composition
# --------------------------------------------------------------------------- #


async def test_aclose_closes_the_backends_that_hold_resources() -> None:
    primary = FakeBackend("jev")
    fallback = FakeBackend("small_llm")
    await ResilientRouter(primary=primary, fallback=fallback).aclose()
    assert primary.closed == 1
    assert fallback.closed == 1


async def test_aclose_tolerates_a_backend_without_cleanup() -> None:
    await ResilientRouter(primary=FakeBackend("jev", closes=False)).aclose()


async def test_aclose_is_safe_with_no_backends_and_safe_twice() -> None:
    router = ResilientRouter(primary=FakeBackend("jev"))
    await router.aclose()
    await router.aclose()
    await ResilientRouter().aclose()


async def test_a_backend_that_fails_to_close_does_not_break_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="paper_agent.routing.resilience")

    class _BadClose(FakeBackend):
        async def aclose(self) -> None:
            raise RuntimeError("socket already gone")

    await ResilientRouter(primary=_BadClose("jev")).aclose()
    assert "routing backend close failed" in caplog.text


def test_make_router_without_a_key_builds_a_fallback_only_router(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="paper_agent.routing.resilience")
    router = make_router(jev_api_key=None)
    assert "no routing backend configured" in caplog.text
    assert router.breaker.state == "closed"


def test_make_router_wires_the_fallback_model() -> None:
    from paper_agent.routing.small_llm import SmallLLMBackend
    from paper_agent.testing import ScriptedChatModel

    router = make_router(jev_api_key=None, fallback_model=ScriptedChatModel(responses=[]))
    assert isinstance(router._fallback, SmallLLMBackend)  # noqa: SLF001


def test_make_router_wires_jev_when_a_key_is_present() -> None:
    from paper_agent.routing.jev import JevBackend

    router = make_router(jev_api_key="test-key", cache=MemoryCache())
    assert isinstance(router._primary, JevBackend)  # noqa: SLF001


def test_make_router_passes_the_breaker_settings_through(clock: FakeClock) -> None:
    router = make_router(jev_api_key=None, failure_threshold=7, recovery_timeout=12.5)
    for _ in range(6):
        router.breaker.record_failure()
    assert router.breaker.state == "closed"
    router.breaker.record_failure()
    assert router.breaker.state == "open"


async def test_every_row_of_the_eval_corpus_produces_a_well_formed_decision() -> None:
    """The D4 acceptance criterion, offline.

    Runs the whole dataset through both backends with canned answers, which proves
    the plumbing survives every shape the corpus contains -- Chinese, English,
    empty ui_context, populated ui_context, injection strings -- rather than
    proving the routing is *correct*. Correctness needs a live key and belongs to
    the D7 eval run; this is the gate that says the harness itself is not the
    reason a row failed.
    """
    rows = _eval_rows()
    assert len(rows) >= 20, "the D4 acceptance criterion is 20 cases"

    jev = _CannedJevBackend()
    small = SmallLLMBackend(
        ScriptedChatModel(
            responses=[
                AIMessage(
                    content=json.dumps(
                        {
                            "intent": "polish",
                            "path_deep": False,
                            "needs_clarify": True,
                            "confidence": 0.6,
                        }
                    )
                )
            ]
        )
    )

    for row in rows:
        request = RouteInput(query=row["query"], ui_context=row["ui_context"])

        jev_decisions = await jev.decide(request)
        assert jev_decisions.intent in INTENTS, row["query"]
        assert jev_decisions.source == "jev"

        small_decisions = await small.decide(request)
        assert small_decisions.intent in INTENTS, row["query"]
        assert small_decisions.source == "small_llm"

    await jev.aclose()
