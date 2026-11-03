"""Graph-level tests for the D10 skill injection.

`route` 把映射写进 `state["skill"]`，`agent` 把 pack 注入为第二条 SystemMessage。
pack 的落点要用**捕获型假模型**断言（记录 ainvoke 收到的完整消息列表）——这验证的
不是「函数返回了 pack」，而是「模型真的在正确位置收到了它」。知识问答路径不叠
skill；裸环没有意图信号，catalog 在也白搭，一并钉住。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from paper_agent.graph import build_graph
from paper_agent.knowledge import bind_knowledge
from paper_agent.routing.decision_model import Decisions
from paper_agent.skill_loader import SkillCatalog
from paper_agent.testing import ScriptedRouter

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"

REPLY = "好的。"


class _RecordingModel:
    """只实现 bind_tools + ainvoke 的假模型：记录每次收到的消息列表。"""

    def __init__(self, reply: str = REPLY) -> None:
        self._reply = reply
        self.calls: list[list[Any]] = []

    def bind_tools(self, tools: Sequence[Any], **_kwargs: Any) -> _RecordingModel:
        return self

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(list(messages))
        return AIMessage(content=self._reply)


def _decisions(intent: str) -> Decisions:
    return Decisions(
        intent=intent,  # type: ignore[arg-type]
        needs_clarify=False,
        path_deep=False,
        confidence=0.9,
        source="jev",  # type: ignore[arg-type]
    )


def _catalog() -> SkillCatalog:
    if not VENDOR_ROOT.is_dir():
        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    return SkillCatalog(VENDOR_ROOT)


def _graph(model: Any, *, router: Any = None, catalog: Any = None, **kwargs: Any) -> Any:
    return build_graph(
        model=model,
        tools=[],
        checkpointer=InMemorySaver(),
        router=router,
        skill_catalog=catalog,
        **kwargs,
    )


def _cfg() -> dict[str, Any]:
    return {"configurable": {"thread_id": "t"}}


async def test_mapped_intent_injects_the_pack_in_position_two() -> None:
    model = _RecordingModel()
    catalog = _catalog()
    graph = _graph(model, router=ScriptedRouter(_decisions("polish")), catalog=catalog)
    out = await graph.ainvoke({"messages": [("user", "润色这段")]}, _cfg())

    assert out["skill"] == "nature-polishing"
    assert out["intent"] == "polish"
    messages = model.calls[0]
    assert isinstance(messages[0], SystemMessage)  # 主 system prompt
    assert isinstance(messages[1], SystemMessage) and "nature-polishing" in messages[1].content
    assert "【skill 加载：nature-polishing】" in messages[1].content
    # pack 之后才是对话历史。
    assert not isinstance(messages[2], SystemMessage)


async def test_writing_pack_carries_the_narrowing_into_the_graph() -> None:
    model = _RecordingModel()
    graph = _graph(model, router=ScriptedRouter(_decisions("draft_section")), catalog=_catalog())
    out = await graph.ainvoke({"messages": [("user", "帮我写方法节")]}, _cfg())

    assert out["skill"] == "nature-writing"
    pack = model.calls[0][1].content
    assert "优先于下文 skill 内容" in pack
    assert "章节级" in pack


async def test_unmapped_intent_writes_no_skill_and_no_pack() -> None:
    model = _RecordingModel()
    graph = _graph(
        model, router=ScriptedRouter(_decisions("literature_search")), catalog=_catalog()
    )
    out = await graph.ainvoke({"messages": [("user", "找几篇论文")]}, _cfg())

    assert out["skill"] is None
    messages = model.calls[0]
    assert isinstance(messages[1], AIMessage) or not isinstance(messages[1], SystemMessage)
    assert all(not (isinstance(m, SystemMessage) and "skill 加载" in m.content) for m in messages)


async def test_catalog_none_is_the_pre_d10_behaviour() -> None:
    model = _RecordingModel()
    graph = _graph(model, router=ScriptedRouter(_decisions("polish")), catalog=None)
    out = await graph.ainvoke({"messages": [("user", "润色这段")]}, _cfg())

    assert out["skill"] is None
    assert all(
        not (isinstance(m, SystemMessage) and "skill 加载" in m.content) for m in model.calls[0]
    )


async def test_knowledge_path_never_gets_a_skill_pack() -> None:
    model = _RecordingModel()
    graph = _graph(model, catalog=_catalog(), knowledge_model=model)
    with bind_knowledge([{"source_id": "K1", "text": "资料片段"}]):
        out = await graph.ainvoke({"messages": [("user", "资料里怎么说")]}, _cfg())

    assert out["intent"] == "knowledge_qa"
    assert out["skill"] is None
    assert all(
        not (isinstance(m, SystemMessage) and "skill 加载" in m.content) for m in model.calls[0]
    )


async def test_bare_loop_ignores_the_catalog() -> None:
    """裸环没有意图信号：无 route 节点 → 无 intent → pack 无从谈起。可达性以路由
    为前提是特性——零配置启动多一个 catalog 也不会改变行为。"""
    model = _RecordingModel()
    graph = _graph(model, catalog=_catalog())
    out = await graph.ainvoke({"messages": [("user", "随便聊聊")]}, _cfg())

    assert "skill" not in out
    assert all(
        not (isinstance(m, SystemMessage) and "skill 加载" in m.content) for m in model.calls[0]
    )
