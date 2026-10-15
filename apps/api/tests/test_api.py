"""HTTP contract for the gateway.

These pin the wire format the Next.js client depends on. D2 turned /api/chat
into an SSE stream, so the D1 echo assertions were rewritten — deliberately,
which is the whole reason they existed. When D3 swaps the fake generator for the
LangGraph agent, the tests that hardcode the echo text are the ones that should
change; the framing tests (headers, chunking, `done`, 422-stays-JSON) should not.
"""

from __future__ import annotations

import json
import re

import pytest
from api.config import get_settings
from api.main import app
from api.routes import chat as chat_route
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_stream_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake generator paces itself at 40ms/slice for the browser demo.

    Zeroed here so ~30 slices per request do not add a second to every streaming
    test. The delay is cosmetic; nothing about the wire format depends on it.
    """
    monkeypatch.setattr(chat_route, "CHUNK_DELAY", 0)


# --------------------------------------------------------------------------- #
# SSE helpers
# --------------------------------------------------------------------------- #


def parse_sse(raw: bytes) -> list[tuple[str, str]]:
    """Parse an SSE body into (event, data) pairs.

    An independent re-implementation of apps/web/src/lib/sseTransport.ts rather
    than shared code, on purpose: when the TypeScript client and this Python test
    agree on the same bytes, the wire format is genuinely well defined instead of
    accidentally matching whichever implementation was written first.

    Handles the three line endings the spec allows (sse-starlette defaults to
    \\r\\n), skips comment lines (that is how the 15s keepalive ping arrives),
    joins multi-line data fields, and strips exactly one space after the colon.
    """
    events: list[tuple[str, str]] = []
    for block in re.split(r"\r\n\r\n|\n\n|\r\r", raw.decode("utf-8")):
        name, data_lines = "message", []
        for line in re.split(r"\r\n|\n|\r", block):
            if not line or line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                name = value
            elif field == "data":
                data_lines.append(value)
        if data_lines:
            events.append((name, "\n".join(data_lines)))
    return events


def tokens(events: list[tuple[str, str]]) -> str:
    return "".join(json.loads(data)["text"] for name, data in events if name == "token")


def user_msg(text: str, **extra: object) -> dict:
    """The body the AI SDK transport sends: a `messages` array, not a `message`."""
    return {
        "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": text}]}],
        **extra,
    }


def stream(body: dict) -> list[tuple[str, str]]:
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    return parse_sse(r.content)


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #


def test_routes_are_registered() -> None:
    """Asserted via the OpenAPI schema, NOT by walking app.routes.

    FastAPI 0.143's include_router wraps sub-routers in an `_IncludedRouter`
    instead of flattening them, so `[r.path for r in app.routes]` no longer
    contains /api/chat and will falsely report the router as missing.
    """
    paths = set(app.openapi()["paths"])
    assert {"/health", "/api/chat"} <= paths


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #


def test_health_shape() -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert set(body["backends"]) == {"checkpoint", "cache", "blob"}
    assert set(body["llm"]) == {
        "base_url",
        "chat_model",
        "reasoning_model",
        "key_present",
    }
    assert isinstance(body["llm"]["key_present"], bool)


def test_health_does_not_leak_the_api_key() -> None:
    """/health is polled by the browser on every page load and is the first thing
    anyone curls when debugging. key_present is a bool for exactly this reason.
    """
    settings = get_settings()
    text = client.get("/health").text
    if settings.llm_api_key:
        assert settings.llm_api_key.get_secret_value() not in text
    assert "sk-" not in text
    assert "**********" not in text, "masking placeholder implies the field was serialized"


def test_health_reflects_configured_models() -> None:
    settings = get_settings()
    llm = client.get("/health").json()["llm"]
    assert llm["chat_model"] == settings.llm_chat_model
    assert llm["reasoning_model"] == settings.llm_reasoning_model
    assert llm["key_present"] == bool(settings.llm_api_key)


# --------------------------------------------------------------------------- #
# /api/chat — SSE framing. This is the D2 contract.
# --------------------------------------------------------------------------- #


def test_chat_streams_sse_headers() -> None:
    r = client.post("/api/chat", json=user_msg("hi"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # sse-starlette sets both of these. Asserting them pins what the browser
    # actually needs to render progressively: no-store keeps a cache from
    # replaying a stale stream, and X-Accel-Buffering stops nginx (Week 4) from
    # holding the whole response back until the generator finishes.
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-accel-buffering"] == "no"


def test_chat_stream_is_chunked_not_buffered() -> None:
    """The actual point of D2.

    Many separate `token` events, not one body at the end. If the generator were
    ever collected into a single response the UI would still show the right text
    and every other test here would pass — the stream would just arrive all at
    once. This is the only test that would notice.
    """
    events = stream(user_msg("帮我把参考文献改成国标格式"))
    assert sum(1 for name, _ in events if name == "token") > 1


def test_chat_stream_ends_with_exactly_one_done() -> None:
    """The client reads a missing `done` as a truncated stream and shows an error,
    so `done` has to be both last and unique."""
    events = stream(user_msg("hi"))
    assert events[-1][0] == "done"
    assert sum(1 for name, _ in events if name == "done") == 1


def test_chat_stream_reassembles_the_echo() -> None:
    """Concatenated deltas must equal the reply exactly — this is what catches a
    dropped, duplicated or reordered chunk. Hardcodes the D2 placeholder text on
    purpose; D3 rewrites it along with the generator."""
    text = tokens(stream(user_msg("帮我把参考文献改成国标格式")))
    assert text == (
        "[D2 stream] 收到：帮我把参考文献改成国标格式\nthread_id = (none)\nui_context = {}"
    )


def test_chat_stream_sends_utf8_not_escaped() -> None:
    """ensure_ascii=False on the gateway side. The payload is Chinese, so the
    default would triple the byte count and make DevTools unreadable.

    Asserted on the reassembled text, not on contiguous raw bytes: the generator
    slices every 4 code points, so "对比学习" arrives split across two events with
    SSE framing in between. Slicing by code point is what guarantees a multi-byte
    character is never cut in half.
    """
    r = client.post("/api/chat", json=user_msg("对比学习"))
    assert "对比学习" in tokens(parse_sse(r.content))
    assert b"\\u" not in r.content


def test_chat_stream_carries_thread_id() -> None:
    """Pins the `id` -> `thread_id` alias. The SDK calls it `id`; LangGraph's
    checkpointer will want exactly `thread_id` on D7, and this is the seam."""
    text = tokens(stream(user_msg("hi", id="abc-123")))
    assert "thread_id = abc-123" in text


def test_chat_thread_id_is_optional() -> None:
    text = tokens(stream(user_msg("hi")))
    assert "thread_id = (none)" in text


def test_chat_stream_reflects_ui_context() -> None:
    """ui_context is the cheapest strong routing signal available (editor panel,
    cursor section, selection) — it has to survive the trip to the agent."""
    text = tokens(
        stream(
            user_msg(
                "这段太啰嗦了",
                ui_context={
                    "active_panel": "editor",
                    "cursor_section": "3.2",
                    "selected_text": "在这项研究当中……",
                    "paper_stage": "drafting",
                },
            )
        )
    )
    assert '"active_panel":"editor"' in text
    assert '"cursor_section":"3.2"' in text


def test_chat_picks_the_newest_user_turn() -> None:
    """The transport sends the entire history every time. Routing has to look at
    the latest user turn, not the first, or every follow-up re-routes on stale
    text — the classic multi-turn agent bug."""
    body = {
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "第一条"}]},
            {"id": "m2", "role": "assistant", "parts": [{"type": "text", "text": "回复"}]},
            {"id": "m3", "role": "user", "parts": [{"type": "text", "text": "第二条"}]},
        ]
    }
    text = tokens(stream(body))
    assert "第二条" in text
    assert "第一条" not in text


def test_chat_extracts_only_text_parts() -> None:
    """User turns can carry file and custom parts alongside text, and assistant
    turns carry reasoning and tool parts. Extraction must take the text and
    ignore the rest instead of choking on a missing or non-string `text` key."""
    body = {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "parts": [
                    {"type": "reasoning", "text": "内部推理不该出现"},
                    {"type": "tool-search", "toolCallId": "t1", "state": "output-available"},
                ],
            },
            {
                "id": "m2",
                "role": "user",
                "parts": [
                    {
                        "type": "file",
                        "url": "http://example.com/paper.pdf",
                        "mediaType": "application/pdf",
                    },
                    {"type": "custom.route", "data": {"text": "decoy"}},
                    {"type": "text", "text": 12345},
                    {"type": "text", "text": "真实提问"},
                ],
            },
        ]
    }
    text = tokens(stream(body))
    assert "真实提问" in text
    for absent in ("内部推理不该出现", "decoy", "12345", "paper.pdf"):
        assert absent not in text


def test_chat_stream_is_deterministic_and_makes_no_llm_call() -> None:
    """D2 must not touch the network. Two identical requests producing identical
    text is the proof; a live model call would not be stable."""
    payload = user_msg("同样的输入")
    assert tokens(stream(payload)) == tokens(stream(payload))


def test_chat_accepts_regenerate_trigger() -> None:
    """The frontend's regenerate button sends trigger=regenerate-message. It must
    not 422, or the button silently breaks."""
    assert (
        client.post(
            "/api/chat", json=user_msg("再来一次", trigger="regenerate-message")
        ).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# Validation — 422, not 500, and not a silent accept
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("no messages key", {}),
        ("empty history", {"messages": []}),
        (
            "no user turn",
            {
                "messages": [
                    {"id": "a", "role": "assistant", "parts": [{"type": "text", "text": "hi"}]}
                ]
            },
        ),
        ("user turn with no parts", {"messages": [{"id": "a", "role": "user", "parts": []}]}),
        (
            "whitespace-only user turn",
            {"messages": [{"id": "a", "role": "user", "parts": [{"type": "text", "text": "   "}]}]},
        ),
        (
            "over the length cap",
            {
                "messages": [
                    {"id": "a", "role": "user", "parts": [{"type": "text", "text": "x" * 8001}]}
                ]
            },
        ),
        ("bad role", {"messages": [{"id": "a", "role": "banana", "parts": []}]}),
        ("message missing role", {"messages": [{"id": "a", "parts": []}]}),
        ("messages not a list", {"messages": "not-a-list"}),
        ("bad trigger", {**user_msg("hi"), "trigger": "nonsense"}),
    ],
)
def test_chat_rejects_bad_input(label: str, payload: dict) -> None:
    r = client.post("/api/chat", json=payload)
    assert r.status_code == 422, f"{label}: got {r.status_code}"
    # Validation failures must stay JSON. If one ever came back as a 200 SSE
    # stream the browser would render an empty assistant bubble and the real
    # reason would be invisible.
    assert r.headers["content-type"].startswith("application/json"), label


def test_chat_accepts_the_maximum_length() -> None:
    """Pin both ends of the cap so the boundary cannot drift unnoticed."""
    assert (
        client.post("/api/chat", json=user_msg("x" * chat_route.MAX_USER_CHARS)).status_code == 200
    )
    over = user_msg("x" * (chat_route.MAX_USER_CHARS + 1))
    assert client.post("/api/chat", json=over).status_code == 422


def test_chat_rejects_malformed_json() -> None:
    r = client.post(
        "/api/chat",
        content=b'{"messages": [{"id": "a", "role": "user"',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_chat_rejects_wrong_method() -> None:
    assert client.get("/api/chat").status_code == 405


# --------------------------------------------------------------------------- #
# CORS — the browser sends a preflight before any JSON POST
# --------------------------------------------------------------------------- #


def test_cors_preflight_allows_the_web_origin() -> None:
    r = client.options(
        "/api/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "POST" in r.headers["access-control-allow-methods"]


def test_cors_header_present_on_a_streaming_response() -> None:
    """Worth its own test: CORSMiddleware has to still stamp the header on an
    EventSourceResponse. If it did not, the browser would block the stream and
    it would look like a frontend bug."""
    r = client.post(
        "/api/chat",
        json=user_msg("hi"),
        headers={"Origin": "http://localhost:3000"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #


def test_lifespan_builds_every_backend() -> None:
    """Backends are built in lifespan, not at import time, so a misconfigured
    CHECKPOINT_BACKEND fails at startup instead of on the first user request.
    This test is what makes that claim true.
    """
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert app.state.cache is not None
        assert app.state.blobs is not None
        assert app.state.checkpointer is not None


def test_lifespan_propagates_a_bad_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd CHECKPOINT_BACKEND must kill startup, not boot a half-working app.

    Patched at the factory rather than via env vars: get_settings() is lru_cached
    and api.main binds it at import time, so monkeypatch.setenv alone cannot
    change what an already-imported app sees.
    """
    import api.main as main_module

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("unknown CHECKPOINT_BACKEND: 'mongodb'")

    monkeypatch.setattr(main_module, "make_checkpointer", _boom)

    with pytest.raises(ValueError, match="CHECKPOINT_BACKEND"), TestClient(main_module.app):
        pass
