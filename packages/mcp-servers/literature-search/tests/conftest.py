"""Shared fixtures: real captured payloads, a recording MockTransport, offline settings.

`settings` points both base URLs at `.test` domains on purpose. If a test forgets
to pass the transport, the call fails on DNS resolution instead of quietly reaching
api.openalex.org -- which would spend a shared keyless budget measured in cents per
day, and would make the test pass or fail depending on who else is on the network.

The helpers are fixtures rather than imports: no `tests/` directory in this repo is
a package, so `from .conftest import ...` would not resolve.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp_literature_search.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"

#: A route target: a JSON body, a prebuilt Response, an Exception to raise, or a
#: callable taking the request and returning one of those. The callable form is what
#: lets a test serve one path twice with different bodies.
Route = Any | Callable[[httpx2.Request], Any]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def search_payload() -> dict[str, Any]:
    """`GET /works?search=contrastive learning`, captured live 2026-10-19."""
    return _load("search_works.json")


@pytest.fixture
def detail_payload() -> dict[str, Any]:
    """`GET /works/W3133849783`, captured live 2026-10-19, verbatim."""
    return _load("work_detail.json")


@pytest.fixture
def references_payload() -> dict[str, Any]:
    """Synthetic, and built to break two specific things; see fixtures/README.md."""
    return _load("references_batch.json")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        openalex_base_url="https://openalex.test",
        semanticscholar_base_url="https://semanticscholar.test",
        timeout_seconds=5.0,
    )


@pytest.fixture
def make_transport() -> Callable[
    [dict[str, Route]], tuple[httpx2.MockTransport, list[httpx2.Request]]
]:
    """Build a MockTransport plus the list of requests it received.

    Returning the request log alongside the transport is the point: half of what
    these tests assert is what the client *asked for* -- the `select` field list,
    the `from_publication_date` filter, whether a credential went in a header or a
    query parameter -- and none of that is observable from the response.

    Unrouted paths answer 404 rather than hanging, so a typo in a route key surfaces
    as an `UpstreamError` naming the path instead of a test passing for the wrong
    reason.
    """

    def build(routes: dict[str, Route]) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            target = routes.get(request.url.path)
            if callable(target):
                target = target(request)
            if target is None:
                return httpx2.Response(404, json={"error": f"no route for {request.url.path}"})
            if isinstance(target, Exception):
                raise target
            if isinstance(target, httpx2.Response):
                return target
            return httpx2.Response(200, json=target)

        return httpx2.MockTransport(handler), seen

    return build


@pytest.fixture
def rate_limited() -> Callable[..., httpx2.Response]:
    """A 429 shaped like the real one: `retryAfter` in the body, not in a header.

    Transcribed from a live OpenAlex response, minus the paragraph of prose addressed
    to AI agents that the real one carries -- see fixtures/README.md.
    """

    def build(retry_after: int = 66013, service: str = "OpenAlex") -> httpx2.Response:
        return httpx2.Response(
            429,
            json={
                "error": "Rate limit exceeded",
                "message": f"Insufficient budget; the free daily {service} budget is used up.",
                "retryAfter": retry_after,
                "costUsd": 0.001,
                "dailyRemainingUsd": 0,
            },
        )

    return build
