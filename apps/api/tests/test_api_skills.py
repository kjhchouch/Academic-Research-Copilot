"""D10 的 HTTP 契约：route 事件携带 skill、/health 暴露可达性。

与 test_api_grounding.py 同一套手法：真编译图 + ScriptedRouter +
dependency_overrides，只有 LLM 调用是假的。skill pack 的注入断言在包级
（test_graph_skills.py 用捕获型假模型）；这里验证的是网关侧的两件事——
route 事件把 skill 名带给前端（additive 字段），/health 让 vendor 缺失可见。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from api.main import app
from api.routes import chat as chat_route
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from paper_agent.graph import build_graph
from paper_agent.routing.decision_model import Decisions
from paper_agent.skill_loader import SkillCatalog
from paper_agent.testing import ScriptedChatModel, ScriptedRouter

client = TestClient(app)

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"


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


def _install(router: Any) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="润色建议如下。")])
    catalog = SkillCatalog(VENDOR_ROOT) if VENDOR_ROOT.is_dir() else None
    graph = build_graph(
        model=model.bind_tools([]),
        tools=[],
        checkpointer=None,
        router=router,
        skill_catalog=catalog,
    )
    app.dependency_overrides[chat_route.get_agent] = lambda: graph


def _decisions(intent: str) -> Decisions:
    return Decisions(
        intent=intent,  # type: ignore[arg-type]
        needs_clarify=False,
        path_deep=False,
        confidence=0.9,
        source="jev",  # type: ignore[arg-type]
    )


def test_route_event_carries_the_mapped_skill() -> None:
    if not VENDOR_ROOT.is_dir():
        import pytest

        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    _install(ScriptedRouter(_decisions("polish")))
    try:
        r = client.post("/api/chat", json=_user_msg("润色这段"))
        assert r.status_code == 200, r.text
        events = _parse_sse(r.content)
        route_payload = json.loads(next(data for name, data in events if name == "route"))
        # D10 的 additive 字段：原七键一个不少，skill 是第八个。
        assert set(route_payload) == {
            "intent",
            "guard",
            "needs_clarify",
            "path_deep",
            "source",
            "confidence",
            "from_cache",
            "skill",
        }
        assert route_payload["skill"] == "nature-polishing"
        assert route_payload["guard"] == "proceed"
    finally:
        app.dependency_overrides.clear()


def test_route_event_skill_is_null_for_unmapped_intent() -> None:
    _install(ScriptedRouter(_decisions("literature_search")))
    try:
        r = client.post("/api/chat", json=_user_msg("找几篇论文"))
        assert r.status_code == 200, r.text
        events = _parse_sse(r.content)
        route_payload = json.loads(next(data for name, data in events if name == "route"))
        assert route_payload["skill"] is None
    finally:
        app.dependency_overrides.clear()


def test_health_exposes_the_skills_block() -> None:
    """无 lifespan 的 TestClient 读不到 app.state，reachable 走防御性 0——这个块要
    保证的是**形状**与「vendor 缺失可见」这条语义；reachable==3 的真值由 catalog
    自己的测试与真实启动日志负责。"""
    body = client.get("/health").json()
    assert set(body["skills"]) == {"enabled", "reachable"}
    assert isinstance(body["skills"]["enabled"], bool)
    assert isinstance(body["skills"]["reachable"], int)
    assert 0 <= body["skills"]["reachable"] <= 3
