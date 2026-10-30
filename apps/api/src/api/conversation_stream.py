from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio


class TurnRecorder:
    def __init__(self) -> None:
        self.message: dict[str, Any] = {"id": uuid.uuid4().hex, "role": "assistant", "parts": []}
        self.status = "interrupted"
        self.error: str | None = None

    def consume(self, event: dict[str, str]) -> None:
        name, data = event["event"], json.loads(event["data"])
        parts = self.message["parts"]
        if name == "token":
            if parts and parts[-1]["type"] == "text":
                parts[-1]["text"] += data["text"]
            else:
                parts.append({"type": "text", "text": data["text"]})
        elif name in ("route", "citation", "knowledge-source", "knowledge-coverage"):
            key = {
                "route": "route",
                "citation": f"citation-{data.get('citation_id')}",
                "knowledge-source": f"knowledge-{data.get('source_id')}",
                "knowledge-coverage": "knowledge-coverage",
            }[name]
            parts.append({"type": f"data-{name}", "id": key, "data": data})
        elif name == "tool":
            if data["status"] == "start":
                parts.append(
                    {
                        "type": "dynamic-tool",
                        "toolCallId": data["id"],
                        "toolName": data["name"],
                        "state": "input-available",
                        "input": data.get("args"),
                    }
                )
            else:
                for part in parts:
                    if part.get("toolCallId") == data["id"]:
                        part.update(state="output-available", output=data.get("result"))
                        break
        elif name == "done":
            self.status = "complete"
        elif name == "error":
            self.status = "error"
            self.error = data.get("message", "回答失败")

    def snapshot(self) -> dict[str, Any]:
        status: dict[str, str] = {"status": self.status}
        if self.error:
            status["message"] = self.error
        return {
            **self.message,
            "parts": [
                *self.message["parts"],
                {"type": "data-turn-status", "id": "turn-status", "data": status},
            ],
        }


async def record_stream(
    events: AsyncIterator[dict[str, str]],
    store: Any,
    project_id: str,
    session_id: str,
    user_message: dict[str, Any],
    active_sessions: set[str],
) -> AsyncIterator[dict[str, str]]:
    recorder = TurnRecorder()
    last_saved = time.monotonic()
    started = False
    try:
        await asyncio.to_thread(store.save_message, project_id, session_id, user_message)
        await asyncio.to_thread(store.save_message, project_id, session_id, recorder.snapshot())
        started = True
        yield {"event": "message-id", "data": json.dumps({"id": recorder.message["id"]})}
        async for event in events:
            recorder.consume(event)
            terminal = event["event"] in ("done", "error")
            if terminal or time.monotonic() - last_saved > 0.5:
                await asyncio.to_thread(
                    store.save_message, project_id, session_id, recorder.snapshot()
                )
                last_saved = time.monotonic()
            if terminal:
                yield {
                    "event": "turn-status",
                    "data": json.dumps(
                        recorder.snapshot()["parts"][-1]["data"], ensure_ascii=False
                    ),
                }
            yield event
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await events.aclose()
                if started:
                    await asyncio.to_thread(
                        store.save_message, project_id, session_id, recorder.snapshot()
                    )
            finally:
                active_sessions.discard(session_id)
