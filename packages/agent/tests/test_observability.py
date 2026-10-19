"""observability.py invariants.

The rule under test is that tracing is optional infrastructure: it must never
raise, never block startup, and never send anything anywhere when it is not
configured. Those are the properties that let the app boot with zero external
services, which is the whole local-first premise.

No test here constructs a real Langfuse client on purpose. Doing so registers a
process-wide OpenTelemetry tracer provider, so one test's spans would be exported
by every later test in the session -- a leak that shows up as mysterious network
traffic rather than as a failure. The real-client path is covered by the live
end-to-end run against DeepSeek instead, which is the only place a genuine
export can be observed anyway.
"""

from __future__ import annotations

from typing import Any

import pytest
from paper_agent.observability import (
    SESSION_ID_KEY,
    TAGS_KEY,
    TRACE_NAME_KEY,
    USER_ID_KEY,
    make_tracer,
    shutdown_tracer,
    trace_metadata,
    tracer_callbacks,
    verify_tracer,
)


class _RaisingClient:
    """Stands in for an unreachable or misconfigured Langfuse."""

    def auth_check(self) -> bool:
        raise ConnectionError("https://us.cloud.langfuse.com: name or service not known")

    def shutdown(self) -> None:
        raise RuntimeError("exporter already closed")


class _WorkingClient:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def auth_check(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1


# --------------------------------------------------------------------------- #
# Off means off
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("public_key", "secret_key"),
    [(None, None), ("pk-lf-x", None), (None, "sk-lf-x"), ("", ""), ("pk-lf-x", "")],
)
def test_make_tracer_is_none_without_both_keys(
    public_key: str | None, secret_key: str | None
) -> None:
    """Either key missing disables tracing entirely.

    Half-configured is the state a copied `.env.example` leaves you in, and a
    client built from it would fail on every export instead of staying quiet.
    """
    assert (
        make_tracer(
            public_key=public_key,
            secret_key=secret_key,
            host="https://us.cloud.langfuse.com",
        )
        is None
    )


def test_tracer_callbacks_is_empty_when_disabled() -> None:
    """The gateway adds this to every request config, so `None` has to produce an
    empty list rather than something LangChain would choke on."""
    assert tracer_callbacks(None) == []


def test_shutdown_tracer_tolerates_none_and_failure() -> None:
    """Shutdown runs on the way out of the lifespan, where an exception would
    mask whatever actually caused the shutdown."""
    shutdown_tracer(None)
    shutdown_tracer(_RaisingClient())
    client = _WorkingClient()
    shutdown_tracer(client)
    assert client.shutdown_calls == 1


class _Manager:
    """Stands in for Langfuse's per-public-key `LangfuseResourceManager`."""


class _CountingClient:
    """A client that shares its `_resources` manager, like two `Langfuse(...)`
    objects built with one public_key do."""

    def __init__(self, manager: object) -> None:
        self._resources = manager
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_shutdown_runs_once_per_shared_resource_manager() -> None:
    """The guard keys on the manager, not the client.

    Two clients share one manager here, exactly as two `Langfuse(...)` with one
    public_key do. The manager's second `shutdown()` would block forever in
    `_score_queue.join()` once its consumers are dead, so the second client --
    a different object, same manager -- must be skipped.
    """
    manager = _Manager()
    first = _CountingClient(manager)
    second = _CountingClient(manager)
    shutdown_tracer(first)
    shutdown_tracer(second)
    assert first.shutdown_calls == 1
    assert second.shutdown_calls == 0


def test_shutdown_runs_once_per_distinct_resource_manager() -> None:
    """Distinct managers are independent, so the guard is not merely a global
    'shut down once' -- two public keys each get their one shutdown."""
    a = _CountingClient(_Manager())
    b = _CountingClient(_Manager())
    shutdown_tracer(a)
    shutdown_tracer(b)
    assert a.shutdown_calls == 1
    assert b.shutdown_calls == 1


# --------------------------------------------------------------------------- #
# Verification is advisory, never fatal
# --------------------------------------------------------------------------- #


def test_verify_tracer_swallows_a_unreachable_backend() -> None:
    """Returns False instead of raising. The caller turns this into one warning
    line; a dead Langfuse must not stop the gateway from serving users."""
    assert verify_tracer(_RaisingClient()) is False


def test_verify_tracer_reports_a_working_backend() -> None:
    assert verify_tracer(_WorkingClient()) is True


# --------------------------------------------------------------------------- #
# Trace metadata — the reserved key names are the contract
# --------------------------------------------------------------------------- #


def test_trace_metadata_sets_thread_id_twice_for_two_reasons() -> None:
    """Plain `thread_id` is the LangGraph resume key the integration reads;
    `langfuse_session_id` is what the UI groups conversations by. Dropping either
    degrades differently: no resume key and turns stop linking, no session id and
    a conversation becomes a pile of unrelated traces."""
    metadata = trace_metadata(thread_id="abc-123", name="chat.fast_path")
    assert metadata["thread_id"] == "abc-123"
    assert metadata[SESSION_ID_KEY] == "abc-123"
    assert metadata[TRACE_NAME_KEY] == "chat.fast_path"


def test_trace_metadata_omits_unset_optional_fields() -> None:
    """An empty `user_id` must not appear as a key. Langfuse would index an empty
    string as a real user, and the dashboard would grow a nameless actor."""
    metadata = trace_metadata(thread_id="t", name="n")
    assert USER_ID_KEY not in metadata
    assert TAGS_KEY not in metadata


def test_trace_metadata_carries_user_tags_and_extras() -> None:
    metadata = trace_metadata(
        thread_id="t",
        name="chat.fast_path",
        user_id="u-1",
        tags=["fast-path"],
        ui_context={"active_panel": "editor"},
    )
    assert metadata[USER_ID_KEY] == "u-1"
    assert metadata[TAGS_KEY] == ["fast-path"]
    # Anything unrecognised lands as ordinary trace metadata, which is how
    # ui_context reaches the dashboard without this module knowing about it.
    assert metadata["ui_context"] == {"active_panel": "editor"}


def test_trace_metadata_extras_cannot_shadow_a_reserved_key() -> None:
    """Explicit arguments win. A caller passing `thread_id=` as an extra would
    otherwise silently re-point the resume key at something else."""
    metadata: dict[str, Any] = trace_metadata(
        thread_id="real", name="n", **{"langfuse_trace_name": "hijack"}
    )
    assert metadata["thread_id"] == "real"
    assert metadata[TRACE_NAME_KEY] == "n"
