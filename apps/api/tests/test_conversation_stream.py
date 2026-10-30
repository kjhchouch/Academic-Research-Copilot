import json

import pytest
from api.conversation_stream import TurnRecorder, record_stream


def event(name, data):
    return {"event": name, "data": json.dumps(data)}


def test_recorder_retains_prose_tools_routes_and_sources():
    recorder = TurnRecorder()
    for item in [
        event("route", {"guard": "proceed"}),
        event("token", {"text": "hello "}),
        event("token", {"text": "world"}),
        event("tool", {"status": "start", "id": "t1", "name": "search", "args": {"q": "abc"}}),
        event("tool", {"status": "end", "id": "t1", "result": {"found": 1}}),
        event("citation", {"citation_id": 1}),
        event("knowledge-source", {"source_id": "K1", "text": "evidence"}),
        event("done", {}),
    ]:
        recorder.consume(item)
    parts = recorder.snapshot()["parts"]
    assert next(p for p in parts if p["type"] == "text")["text"] == "hello world"
    tool = next(p for p in parts if p["type"] == "dynamic-tool")
    assert tool["state"] == "output-available"
    assert tool["output"] == {"found": 1}
    assert parts[-1]["data"]["status"] == "complete"
    assert any(p["type"] == "data-knowledge-source" for p in parts)


class Store:
    def __init__(self):
        self.saved = {}

    def save_message(self, project, session, message):
        self.saved[message["id"]] = json.loads(json.dumps(message))


async def test_record_stream_exposes_persisted_assistant_id_for_adoption():
    store, active = Store(), {"s"}

    async def events():
        yield event("token", {"text": "answer"})
        yield event("done", {})

    items = [
        item
        async for item in record_stream(
            events(), store, "p", "s", {"id": "u", "role": "user"}, active
        )
    ]
    identifiers = [
        json.loads(item["data"])["id"] for item in items if item["event"] == "message-id"
    ]
    assert len(identifiers) == 1
    assert items[0]["event"] == "message-id"
    reply = store.saved[identifiers[0]]
    assert reply["role"] == "assistant"
    assert reply["parts"][-1]["data"]["status"] == "complete"


async def test_record_stream_saves_before_done_and_releases_session():
    store, active = Store(), {"s"}

    async def events():
        yield event("token", {"text": "answer"})
        yield event("done", {})

    stream = record_stream(events(), store, "p", "s", {"id": "u", "role": "user"}, active)
    async for item in stream:
        if item["event"] == "done":
            assert list(store.saved.values())[-1]["parts"][-1]["data"]["status"] == "complete"
    assert not active


async def test_record_stream_marks_partial_when_closed():
    store, active = Store(), {"s"}

    async def events():
        yield event("token", {"text": "partial"})
        yield event("done", {})

    stream = record_stream(events(), store, "p", "s", {"id": "u", "role": "user"}, active)
    assert (await anext(stream))["event"] == "message-id"
    assert (await anext(stream))["event"] == "token"
    await stream.aclose()
    reply = list(store.saved.values())[-1]
    assert reply["parts"][0]["text"] == "partial"
    assert reply["parts"][-1]["data"]["status"] == "interrupted"
    assert not active


async def test_record_stream_releases_session_when_storage_fails():
    class BrokenStore(Store):
        def save_message(self, *args):
            raise OSError("disk full")

    async def events():
        yield event("done", {})

    active = {"s"}
    with pytest.raises(OSError):
        async for _ in record_stream(events(), BrokenStore(), "p", "s", {"id": "u"}, active):
            pass
    assert not active


@pytest.mark.parametrize("terminal", ["done", "error", "interrupted"])
async def test_coverage_shape_persists_even_when_turn_fails_or_is_interrupted(terminal):
    store, active = Store(), {"s"}
    coverage = {
        "task": "compare",
        "documents": [
            {"document_id": "a", "name": "甲.pdf", "source_ids": ["K1"], "status": "retrieved"},
            {"document_id": "b", "name": "乙.md", "source_ids": [], "status": "missing"},
        ],
    }

    async def events():
        yield event("knowledge-coverage", coverage)
        yield event("knowledge-source", {"source_id": "K1", "text": "evidence"})
        yield event("token", {"text": "partial"})
        yield event(terminal, {"message": "upstream failed"} if terminal == "error" else {})

    stream = record_stream(events(), store, "p", "s", {"id": "u", "role": "user"}, active)
    if terminal == "interrupted":
        assert (await anext(stream))["event"] == "message-id"
        assert (await anext(stream))["event"] == "knowledge-coverage"
        await stream.aclose()
    else:
        async for item in stream:
            if item["event"] == terminal:
                assert list(store.saved.values())[-1]["parts"][0]["data"] == coverage
    parts = list(store.saved.values())[-1]["parts"]
    assert parts[0] == {
        "type": "data-knowledge-coverage",
        "id": "knowledge-coverage",
        "data": coverage,
    }
    assert sum(part["type"] == "data-knowledge-coverage" for part in parts) == 1
    assert parts[-1]["data"]["status"] == ("complete" if terminal == "done" else terminal)
    assert not active
