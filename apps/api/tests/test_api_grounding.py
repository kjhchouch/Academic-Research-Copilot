"""`grounding` SSE 事件的 HTTP 契约（D9）。

与 test_api.py 同一套手法：真编译图 + ScriptedChatModel + dependency_overrides，
只有 LLM 调用是假的。registry 由网关自己 bind（那是 `_agent_stream` 的一部分），
所以这里不需要（也不应该）再包 bind_registry——测试能直接跑，本身就证明
registry 的绑定是网关职责而非调用方职责。
"""

from __future__ import annotations

import json
import re
from typing import Any

from api.main import app
from api.routes import chat as chat_route
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from paper_agent.citations import bind_citations
from paper_agent.graph import build_graph
from paper_agent.grounding import L0OnlyBackend
from paper_agent.testing import ScriptedChatModel, tool_call

client = TestClient(app)

SEARCH_RESULT = {
    "papers": [
        {
            "title": "Momentum Contrast for Unsupervised Visual Representation Learning",
            "authors": ["Kaiming He"],
            "year": 2020,
            "doi": "10.1234/moco",
            "abstract": "MoCo 提出动量编码器与对比损失。",
        }
    ]
}


class _ScriptedEntailment:
    name = "scripted"

    def __init__(
        self, verdicts: dict[int, str] | None = None, *, error: Exception | None = None
    ) -> None:
        self._verdicts = verdicts or {}
        self._error = error

    async def judge(self, pairs: list[Any]) -> list[Any]:
        from paper_agent.grounding.entailment import Judgement

        if self._error is not None:
            raise self._error
        return [
            Judgement(
                citation_id=pair.citation_id,
                verdict=self._verdicts.get(pair.citation_id, "supported"),  # type: ignore[arg-type]
                reason="脚本判定",
            )
            for pair in pairs
        ]


def _search_tool() -> Any:
    async def search_papers(query: str) -> dict[str, Any]:
        import json as _json

        return _json.loads(_json.dumps(SEARCH_RESULT))

    return StructuredTool(
        name="search_papers",
        description="检索学术论文",
        coroutine=search_papers,
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


def _install(entailment: Any, final_reply: str) -> None:
    tool = bind_citations([_search_tool()])[0]
    model = ScriptedChatModel(
        responses=[
            tool_call("search_papers", {"query": "对比学习"}, "call_1"),
            AIMessage(content=final_reply),
        ]
    )
    graph = build_graph(
        model=model.bind_tools([tool]),
        tools=[tool],
        entailment=entailment,
    )
    app.dependency_overrides[chat_route.get_agent] = lambda: graph


def _parse_sse(raw: bytes) -> list[tuple[str, str]]:
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


def _user_msg(text: str) -> dict:
    return {"messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": text}]}]}


def _stream(body: dict) -> list[tuple[str, str]]:
    r = client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    return _parse_sse(r.content)


def _grounding_events(events: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [json.loads(data) for name, data in events if name == "grounding"]


def test_grounding_event_order_and_shape() -> None:
    """token 之后、done 之前，一次；形状稳定，每个键都在。"""
    _install(_ScriptedEntailment({1: "supported"}), "MoCo[1] 提出了动量编码器。")
    events = _stream(_user_msg("找对比学习的论文"))

    names = [name for name, _ in events]
    assert names.count("grounding") == 1
    last_token = max(i for i, (name, _) in enumerate(events) if name == "token")
    grounding_index = names.index("grounding")
    assert last_token < grounding_index < names.index("done")

    payload = _grounding_events(events)[0]
    assert set(payload) == {"verdicts", "source"}
    assert payload["source"] == "scripted"
    assert payload["verdicts"] == [{"citation_id": 1, "verdict": "supported", "reason": "脚本判定"}]


def test_fabricated_marker_is_marked_unbacked() -> None:
    """模型编的 `[9]`（工具从没返回过）→ unbacked。这半段不需要模型。"""
    _install(_ScriptedEntailment(), "MoCo[9] 提出了动量编码器。")
    events = _stream(_user_msg("找对比学习的论文"))
    payload = _grounding_events(events)[0]
    assert payload["verdicts"] == [{"citation_id": 9, "verdict": "unbacked", "reason": None}]


def test_no_citations_means_no_grounding_event() -> None:
    """无引用轮次零开销：没有事件，流照常收尾。"""
    _install(_ScriptedEntailment(), "MoCo 是一篇对比学习论文。")
    events = _stream(_user_msg("介绍下对比学习"))
    assert _grounding_events(events) == []
    assert [name for name, _ in events][-1] == "done"


def test_entailment_failure_fails_open_and_the_stream_still_finishes() -> None:
    """后端炸掉 → uncertain + entailment_unavailable，然后照常 done。

    核验是流的增强：一次标注服务的中断绝不能把一条已经答完的流变成 error。
    """
    _install(_ScriptedEntailment(error=RuntimeError("boom")), "MoCo[1] 提出了动量编码器。")
    events = _stream(_user_msg("找对比学习的论文"))
    payload = _grounding_events(events)[0]
    assert payload["verdicts"] == [
        {"citation_id": 1, "verdict": "uncertain", "reason": "entailment_unavailable"}
    ]
    assert [name for name, _ in events][-1] == "done"
    assert not any(name == "error" for name, _ in events)


def test_health_exposes_the_grounding_block() -> None:
    body = client.get("/health").json()
    assert set(body["grounding"]) == {"configured", "backend"}
    assert isinstance(body["grounding"]["configured"], bool)
    assert body["grounding"]["backend"] in {"off", "small_llm"}


def test_off_backend_still_marks_unbacked_through_the_gateway() -> None:
    """GROUNDING_BACKEND=off（L0OnlyBackend）不是全关：unbacked 标注照发，
    source 为 null——纯 L0 轮次的出处必须可辨。"""
    _install(L0OnlyBackend(), "MoCo[9] 提出了动量编码器。")
    events = _stream(_user_msg("找对比学习的论文"))
    payload = _grounding_events(events)[0]
    assert payload["source"] is None
    assert payload["verdicts"] == [{"citation_id": 9, "verdict": "unbacked", "reason": None}]
