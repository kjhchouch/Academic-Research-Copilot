"""Timeout, circuit breaker and cache around the routing backends.

The timeout lives in `jev.py`, because it is a property of one backend's call
and has to be set where the client is constructed. What is here is the part that
has to sit *outside* both backends: deciding when to stop trying the primary, and
when not to ask anyone at all.

Why a breaker rather than just a fallback. With only a fallback, every request
during a Jev outage pays the full 800ms timeout before degrading -- the outage
costs latency on 100% of traffic for as long as it lasts. The breaker converts
that into one probe per recovery window.

Why a cache at all, given routing is cheap. It is not about the money. It is
about determinism: the same message and the same editor context should route the
same way within a short window, and a model call does not guarantee that. A user
who retries a request that got a bad routing should not get a different bad
routing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from paper_agent.routing.decision_model import (
    INTENT_CRITERIA,
    INTENT_INSTRUCTIONS,
    NEEDS_CLARIFY_CRITERIA,
    NEEDS_CLARIFY_INSTRUCTIONS,
    PATH_DEEP_CRITERIA,
    PATH_DEEP_INSTRUCTIONS,
    DecisionModel,
    Decisions,
    DecisionUnavailable,
    RouteInput,
    decisions_from_json,
    decisions_to_json,
    static_fallback,
)
from paper_agent.routing.jev import make_jev_backend
from paper_agent.routing.small_llm import SmallLLMBackend
from paper_agent.storage import Cache

logger = logging.getLogger(__name__)

BreakerState = Literal["closed", "open", "half_open"]

# The plan says TTL 1h. That is long enough to absorb a retry storm and short
# enough that a routing mistake does not stick to a user for a session.
DEFAULT_CACHE_TTL_SECONDS = 3600

# Bumped whenever the question text changes, and folded into the cache key. The
# criteria *are* the prompt: editing one changes what the router means by
# "polish", so decisions cached under the old wording are answers to a different
# question. Without this, tuning the criteria against the eval set would appear
# to do nothing for up to an hour, which is exactly the kind of result that sends
# someone debugging the wrong layer.
_QUESTION_SET = {
    "intent": {"instructions": INTENT_INSTRUCTIONS, "criteria": dict(INTENT_CRITERIA)},
    "path_deep": {"instructions": PATH_DEEP_INSTRUCTIONS, "criteria": PATH_DEEP_CRITERIA},
    "needs_clarify": {
        "instructions": NEEDS_CLARIFY_INSTRUCTIONS,
        "criteria": NEEDS_CLARIFY_CRITERIA,
    },
}

QUESTION_FINGERPRINT = hashlib.sha256(
    json.dumps(_QUESTION_SET, sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:12]


def decision_key(request: RouteInput) -> str:
    """A stable cache key for one routing decision.

    sha256 over canonical JSON, not the builtin `hash()`. `hash()` of a str is
    salted per process by PYTHONHASHSEED, so the same request would produce
    different keys in different workers -- a shared Redis would never hit, a
    restart would orphan every entry, and locally it would look like it worked
    right up until the process count went above one.

    `sort_keys` matters for the same reason: dict ordering follows insertion, and
    a client that sends ui_context keys in a different order is asking the same
    question.
    """
    payload = json.dumps(
        {"v": QUESTION_FINGERPRINT, **request.as_state()},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # Prefixed so this key space cannot collide with anything else sharing the
    # cache instance.
    return f"route:{digest}"


class CircuitBreaker:
    """Consecutive-failure breaker with a single half-open probe.

    Not thread-safe, and deliberately so: it is used from one event loop and none
    of these methods await, so each runs to completion without interleaving. If
    routing ever moves to a thread pool this needs a lock.

    `clock` is injectable because every interesting transition here is a passage
    of time, and a test that sleeps 30 real seconds to observe a recovery is a
    test nobody runs.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._recovery_timeout:
            return "half_open"
        return "open"

    @property
    def failures(self) -> int:
        return self._failures

    def allow_request(self) -> bool:
        """Whether the primary may be tried. Claims the half-open probe slot."""
        state = self.state
        if state == "closed":
            return True
        if state == "open":
            return False
        # Half-open admits exactly one request. Without this, a burst arriving at
        # the recovery boundary would send every request to a backend that is
        # still suspected down -- which is the thundering herd the half-open
        # state exists to prevent, and it would also mean one recovery window
        # produces N failures instead of one.
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if self._probe_in_flight:
            # The probe failed: reopen for another full window. The consecutive
            # count is left at the threshold so `state` stays coherent for anyone
            # reading it in a test or a log.
            self._opened_at = self._clock()
            self._probe_in_flight = False
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()
            logger.warning(
                "routing circuit open after %d consecutive failures; recovering in %.0fs",
                self._failures,
                self._recovery_timeout,
            )


class ResilientRouter:
    """Cache -> primary (behind the breaker) -> fallback -> static.

    Not a `DecisionModel` itself: it has no `name`, because the name belongs to
    whichever backend actually decided, and reporting "router" in the trace would
    hide the one thing worth knowing. The graph node calls this; the eval grades
    `Decisions.source`.

    Every failure mode degrades one step rather than raising. The last step is
    `static_fallback()`, which asks the user to rephrase -- so the worst case for
    a total routing outage is a slightly annoying turn, never a 500.
    """

    def __init__(
        self,
        *,
        primary: DecisionModel | None = None,
        fallback: DecisionModel | None = None,
        cache: Cache | None = None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds
        self._breaker = breaker if breaker is not None else CircuitBreaker()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def decide(self, request: RouteInput) -> Decisions:
        key = decision_key(request)

        cached = await self._read(key)
        if cached is not None:
            return cached

        if self._primary is not None and self._breaker.allow_request():
            try:
                decisions = await self._primary.decide(request)
            except DecisionUnavailable:
                self._breaker.record_failure()
            else:
                self._breaker.record_success()
                await self._write(key, decisions)
                return decisions

        if self._fallback is not None:
            try:
                decisions = await self._fallback.decide(request)
            except DecisionUnavailable:
                logger.warning("routing fallback also unavailable; using static decision")
            else:
                await self._write(key, decisions)
                return decisions

        return static_fallback()

    async def aclose(self) -> None:
        """Release anything the backends hold. Safe with no backends, and twice.

        `aclose` is looked up rather than called because `DecisionModel` does not
        require it -- only the Jev backend holds a socket-backed client, and
        making cleanup part of the Protocol would force the small-model backend to
        implement a no-op method.
        """
        for backend in (self._primary, self._fallback):
            aclose = getattr(backend, "aclose", None)
            if aclose is None:
                continue
            try:
                await aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown must not mask the real error
                logger.warning("routing backend close failed: %s", type(exc).__name__)

    async def _read(self, key: str) -> Decisions | None:
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not break routing
            logger.warning("route cache read failed: %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        decisions = decisions_from_json(raw)
        if decisions is None:
            # Stale schema or a corrupt entry. Logged, because a cache that
            # silently never parses is indistinguishable from one that never
            # hits, and only the first is a bug.
            logger.info("discarding unparsable route cache entry %s", key)
        return decisions

    async def _write(self, key: str, decisions: Decisions) -> None:
        # The static fallback is never cached. It means "every backend failed",
        # so storing it would serve "please rephrase" for a full TTL after the
        # backends have recovered -- an outage that outlives its cause.
        if self._cache is None or decisions.source == "static":
            return
        try:
            await self._cache.set(key, decisions_to_json(decisions), self._cache_ttl)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not break routing
            logger.warning("route cache write failed: %s", type(exc).__name__)


def make_router(
    *,
    jev_api_key: str | None,
    jev_model: str = "jev",
    fallback_model: Any = None,
    cache: Cache | None = None,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    failure_threshold: int = 3,
    recovery_timeout: float = 30.0,
) -> ResilientRouter:
    """Compose the router from configuration, local-first.

    With no Jev key and no fallback model this returns a router that always
    answers `static_fallback()` -- which boots, serves, and tells every user to
    rephrase. That is a worse product than a working router and a better one than
    a gateway that will not start, and it is the same trade `make_tracer` and
    `make_checkpointer` already make.
    """
    primary = make_jev_backend(api_key=jev_api_key, model=jev_model)
    fallback = None if fallback_model is None else SmallLLMBackend(fallback_model)

    if primary is None and fallback is None:
        logger.warning("no routing backend configured; every request will be asked to rephrase")

    return ResilientRouter(
        primary=primary,
        fallback=fallback,
        cache=cache,
        cache_ttl_seconds=cache_ttl_seconds,
        breaker=CircuitBreaker(
            failure_threshold=failure_threshold, recovery_timeout=recovery_timeout
        ),
    )
