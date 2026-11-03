"""Graph-level tests for the D9 `verify` node.

真编译图 + ScriptedChatModel + 脚本化蕴含后端：verify 节点要在图里真正被走到、
把判定写进 state、并且不往 messages 里添任何东西。registry 走真的
`CitationRegistry` + `bind_citations` 包装的异步假工具——unbacked 的判定路径
依赖「工具真返回过的论文」和「模型编的号」之间的差，造不出来就测不到。

`bind_registry` 是网关在生产里包住整条流的东西；测试里必须显式包住 `ainvoke`，
ContextVar 才会流进 ToolNode 的包装工具和 verify 节点。最后一个测试刻意不包，
钉住「没有 registry 就短路」这条契约。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from paper_agent.citations import CitationRegistry, bind_citations, bind_registry
from paper_agent.graph import build_graph
from paper_agent.grounding.entailment import Judgement
from paper_agent.testing import ScriptedChatModel, tool_call


async def _search_papers(query: str) -> dict[str, Any]:
    return {
        "papers": [
            {
                "title": "Momentum Contrast for Unsupervised Visual Representation Learning",
                "authors": ["Kaiming He"],
                "year": 2020,
                "doi": "10.1234/moco",
                "abstract": "MoCo 提出动量编码器与对比损失，在多个视觉任务上验证有效。",
            }
        ]
    }


def _search_tool() -> Any:
    return StructuredTool(
        name="search_papers",
        description="搜索文献",
        coroutine=_search_papers,
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


class _ScriptedEntailment:
    name = "scripted"

    def __init__(
        self, verdicts: dict[int, str] | None = None, *, error: Exception | None = None
    ) -> None:
        self._verdicts = verdicts or {}
        self._error = error
        self.calls: list[list[Any]] = []

    async def judge(self, pairs: Sequence[Any]) -> list[Judgement]:
        self.calls.append(list(pairs))
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


FINAL_WITH_CITATION = "MoCo[1] 提出了动量编码器。"
FINAL_FABRICATED = "MoCo[7] 提出了动量编码器。"
FINAL_PLAIN = "MoCo 是一篇对比学习论文。"
USER_TURN = {"messages": [("user", "找几篇对比学习的论文")]}


def _model(final_reply: str) -> Any:
    return ScriptedChatModel(
        responses=[
            tool_call("search_papers", {"query": "对比学习"}, call_id="call_1"),
            AIMessage(content=final_reply),
        ]
    )


def _graph(entailment: Any, final_reply: str) -> Any:
    return build_graph(
        model=_model(final_reply).bind_tools([]),
        tools=bind_citations([_search_tool()]),
        checkpointer=InMemorySaver(),
        entailment=entailment,
    )


def _cfg(thread_id: str = "t") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def test_cited_answer_gets_grounding_verdicts() -> None:
    entailment = _ScriptedEntailment({1: "supported"})
    graph = _graph(entailment, FINAL_WITH_CITATION)
    with bind_registry(CitationRegistry()):
        out = await graph.ainvoke(USER_TURN, _cfg())

    assert out["grounding"] == [{"citation_id": 1, "verdict": "supported", "reason": "脚本判定"}]
    assert out["grounding_source"] == "scripted"
    # 引用号来自工具真返回的论文：claim 文本里带着 [1]，pair 交给后端。
    assert entailment.calls[0][0].citation_id == 1
    assert "[1]" in entailment.calls[0][0].claim


async def test_verify_appends_no_messages() -> None:
    """核验结论不是对话的一部分：下一轮模型该看到自己的原答案，而不是标注结果。"""
    with bind_registry(CitationRegistry()):
        with_entailment = await _graph(_ScriptedEntailment(), FINAL_WITH_CITATION).ainvoke(
            USER_TURN, _cfg("a")
        )
    with bind_registry(CitationRegistry()):
        without = await _graph(None, FINAL_WITH_CITATION).ainvoke(USER_TURN, _cfg("b"))
    assert len(with_entailment["messages"]) == len(without["messages"])
    assert with_entailment["messages"][-1].content == FINAL_WITH_CITATION


async def test_uncited_answer_writes_no_grounding() -> None:
    entailment = _ScriptedEntailment()
    with bind_registry(CitationRegistry()):
        out = await _graph(entailment, FINAL_PLAIN).ainvoke(USER_TURN, _cfg())
    assert "grounding" not in out
    assert entailment.calls == []


async def test_fabricated_marker_is_unbacked_inside_the_graph() -> None:
    """模型写了 `[7]` 而工具只返回过一篇：L0 查表标注，后端一次都没被调。"""
    entailment = _ScriptedEntailment()
    with bind_registry(CitationRegistry()):
        out = await _graph(entailment, FINAL_FABRICATED).ainvoke(USER_TURN, _cfg())
    assert out["grounding"] == [{"citation_id": 7, "verdict": "unbacked", "reason": None}]
    assert entailment.calls == []


async def test_backend_failure_fails_open_inside_the_graph() -> None:
    """后端炸掉 -> 全部 uncertain，流正常结束。核验是流的增强，不是前提。"""
    entailment = _ScriptedEntailment(error=RuntimeError("boom"))
    with bind_registry(CitationRegistry()):
        out = await _graph(entailment, FINAL_WITH_CITATION).ainvoke(USER_TURN, _cfg())
    assert out["grounding"] == [
        {"citation_id": 1, "verdict": "uncertain", "reason": "entailment_unavailable"}
    ]
    assert out["messages"][-1].content == FINAL_WITH_CITATION


async def test_graph_without_entailment_has_no_verify_node() -> None:
    """D9 之前的图逐字节不变：没有 verify 节点，引用答案也不写 grounding。"""
    graph = _graph(None, FINAL_WITH_CITATION)
    assert "verify" not in graph.get_graph().nodes
    with bind_registry(CitationRegistry()):
        entailment_free = await graph.ainvoke(USER_TURN, _cfg())
    assert "grounding" not in entailment_free


async def test_no_registry_binding_short_circuits_verify() -> None:
    """没有 bind_registry（非聊天场景直调图）时 verify 短路：citations 不是前提。

    此时连 citation 包装工具都拿不到 registry，论文不会注册——若 verify 不短路，
    答案里的 `[1]` 会被误判成 unbacked。短路是唯一正确行为。
    """
    out = await _graph(_ScriptedEntailment(), FINAL_WITH_CITATION).ainvoke(USER_TURN, _cfg())
    assert "grounding" not in out
