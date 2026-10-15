"""Chat endpoint.

D1: echoed a fixed reply, to prove the browser -> FastAPI path works.
D2 (here): SSE stream, still a fake generator -- no LLM yet. The point is to
    prove the streaming pipeline end to end before anything expensive sits
    behind it.
D3: the generator becomes the LangGraph agent's output stream.

Wire protocol
-------------
The gateway emits its own tiny event vocabulary rather than the Vercel AI SDK's
`UIMessageChunk` protocol:

    event: token   data: {"text": "..."}     one text delta
    event: done    data: {}                  stream finished cleanly
    event: error   data: {"message": "..."}  stream failed; no `done` follows

Why not just speak the SDK's protocol from Python: that format is an internal
detail of a TypeScript package and it changes between releases. Keeping the
wire format owned by this service means `apps/web` does the mapping once, in a
typed custom transport that the compiler checks against the installed SDK.
D3 adds `route` / `tool` / `citation` events here without the frontend needing
to know an SDK version bumped.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api", tags=["chat"])

# Longest single user turn we accept. The old D1 model enforced this on
# `message`; now that the transport sends a whole `messages` array the check
# moves onto the extracted text, which is what actually reaches the model.
MAX_USER_CHARS = 8000

# Pacing for the stand-in generator only. In D3 the cadence comes from the model
# itself and these go away. Tests set CHUNK_DELAY to 0 so the suite stays fast.
CHUNK_SIZE = 4
CHUNK_DELAY = 0.04


class UIMessage(BaseModel):
    """One message in the Vercel AI SDK v5+ wire format.

    `parts` is left loosely typed on purpose. User turns carry
    `[{"type": "text", "text": ...}]`, but assistant turns can also carry
    reasoning, tool-call and custom parts. Validating those strictly would
    couple us to an SDK internal we do not own and break the moment a new part
    type appears in a replayed history.
    """

    id: str
    role: Literal["system", "user", "assistant"]
    parts: list[dict[str, Any]] = Field(default_factory=list)


class UiContext(BaseModel):
    """Signals from the editor. Stronger routing features than the text itself."""

    model_config = ConfigDict(extra="allow")

    active_panel: str | None = None
    cursor_section: str | None = None
    selected_text: str | None = None
    paper_stage: str | None = None


class ChatRequest(BaseModel):
    # The SDK names the conversation id `id`; we name it `thread_id` because
    # that is the exact key LangGraph's checkpointer wants on D7. Aliasing here
    # means the D7 diff is zero.
    thread_id: str | None = Field(default=None, alias="id")
    messages: list[UIMessage] = Field(min_length=1)
    trigger: Literal["submit-message", "regenerate-message"] | None = None
    ui_context: UiContext | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    def _require_user_text(self) -> ChatRequest:
        """Reject a history with nothing to route on, with a 422 that says why."""
        text = last_user_text(self.messages)
        if not text:
            raise ValueError("messages contains no user text")
        if len(text) > MAX_USER_CHARS:
            raise ValueError(f"user message exceeds {MAX_USER_CHARS} characters")
        return self


def last_user_text(messages: list[UIMessage]) -> str:
    """Newest user turn's text, concatenated across its text parts."""
    for message in reversed(messages):
        if message.role != "user":
            continue
        text = "".join(
            part.get("text", "")
            for part in message.parts
            if part.get("type") == "text" and isinstance(part.get("text"), str)
        )
        if text.strip():
            return text
    return ""


def _event(name: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
    # ensure_ascii=False: the queries are Chinese, and \uXXXX escapes would
    # triple the byte count and make the stream unreadable in DevTools.
    return {"event": name, "data": json.dumps(payload or {}, ensure_ascii=False)}


async def _fake_stream(req: ChatRequest) -> AsyncIterator[dict[str, str]]:
    """Stand-in for the agent. D3 replaces the body, not the shape."""
    text = last_user_text(req.messages)
    ctx = req.ui_context.model_dump_json(exclude_none=True) if req.ui_context else "{}"
    reply = f"[D2 stream] 收到：{text}\nthread_id = {req.thread_id or '(none)'}\nui_context = {ctx}"

    # Fixed-width slices rather than real tokens: good enough to prove the pipe,
    # and 4 chars keeps Chinese readable as it lands.
    for start in range(0, len(reply), CHUNK_SIZE):
        yield _event("token", {"text": reply[start : start + CHUNK_SIZE]})
        if CHUNK_DELAY:
            await asyncio.sleep(CHUNK_DELAY)

    yield _event("done")


@router.post("/chat")
async def chat_endpoint(req: ChatRequest) -> EventSourceResponse:
    return EventSourceResponse(_fake_stream(req))
