"""`SmallLLMEntailment` 的后端契约。

与 `routing/small_llm.py` 的测试同构：生产传 `make_chat_model(...)`，测试传一个只有
`ainvoke` 的假件——后端对模型的要求就是「能吐字」，这是降级档该有的属性。结构性失败
（JSON 解析不了、index 对不上）抛 `EntailmentUnavailable` 让调用方整批 fail-open；
值级失败（认不出的 verdict）在 pair 已定位的前提下 coerce 成 `uncertain`——那正是
「模型没把握」的诚实标签，不该拖累其余 pair。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import AIMessage
from paper_agent.grounding.entailment import ClaimSpan, EntailmentUnavailable, SmallLLMEntailment


class _FakeModel:
    """只有 `ainvoke` 的假模型：逐次返回 replies 里的文本，记录每次调用。"""

    def __init__(self, replies: list[str], *, delay: float = 0.0, explode: bool = False) -> None:
        self.replies = list(replies)
        self.delay = delay
        self.explode = explode
        self.calls: list[list[dict[str, Any]]] = []

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        if self.explode:
            raise ValueError("upstream exploded")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(json.loads(messages[-1].content))
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return AIMessage(content=reply)


def _pairs(n: int) -> list[ClaimSpan]:
    return [
        ClaimSpan(claim=f"主张{i}", citation_id=i + 1, span=f"来源{i} 说方法有效。")
        for i in range(n)
    ]


def _reply(*items: tuple[int, str, str]) -> str:
    return json.dumps(
        [{"index": i, "verdict": v, "reason": r} for i, v, r in items], ensure_ascii=False
    )


async def test_valid_batch_returns_judgements() -> None:
    model = _FakeModel(
        [_reply((0, "supported", "来源明确陈述"), (1, "contradicted", "来源说相反"))]
    )
    out = await SmallLLMEntailment(model).judge(_pairs(2))
    assert [(j.citation_id, j.verdict, j.reason) for j in out] == [
        (1, "supported", "来源明确陈述"),
        (2, "contradicted", "来源说相反"),
    ]


async def test_fenced_json_is_parsed() -> None:
    model = _FakeModel(
        ["好的，以下是结果：\n```json\n" + _reply((0, "supported", "有依据")) + "\n```"]
    )
    out = await SmallLLMEntailment(model).judge(_pairs(1))
    assert out[0].verdict == "supported"


async def test_malformed_json_raises_unavailable() -> None:
    model = _FakeModel(["这不是 JSON"])
    try:
        await SmallLLMEntailment(model).judge(_pairs(1))
    except EntailmentUnavailable:
        pass
    else:
        raise AssertionError("malformed reply must raise EntailmentUnavailable")


async def test_wrong_item_count_raises_unavailable() -> None:
    """少答一项或多答一项都是结构失配：半可信的结果是没人能消费的状态。"""
    model = _FakeModel([_reply((0, "supported", "有依据"))])
    try:
        await SmallLLMEntailment(model).judge(_pairs(2))
    except EntailmentUnavailable:
        pass
    else:
        raise AssertionError("count mismatch must raise EntailmentUnavailable")


async def test_index_mismatch_raises_unavailable() -> None:
    model = _FakeModel([_reply((7, "supported", "有依据"))])
    try:
        await SmallLLMEntailment(model).judge(_pairs(1))
    except EntailmentUnavailable:
        pass
    else:
        raise AssertionError("index mismatch must raise EntailmentUnavailable")


async def test_out_of_vocabulary_verdict_becomes_uncertain() -> None:
    """`unbacked` 是 L0 查表专属，模型永远不该产它；说了也 coerce 成 uncertain。"""
    model = _FakeModel([_reply((0, "unbacked", "查无此引用"), (1, "TRUE", ""))])
    out = await SmallLLMEntailment(model).judge(_pairs(2))
    assert [j.verdict for j in out] == ["uncertain", "uncertain"]


async def test_long_reason_is_clamped() -> None:
    model = _FakeModel([_reply((0, "supported", "长" * 500))])
    out = await SmallLLMEntailment(model).judge(_pairs(1))
    assert out[0].reason is not None and len(out[0].reason) == 200


async def test_model_failure_raises_unavailable() -> None:
    model = _FakeModel([], explode=True)
    try:
        await SmallLLMEntailment(model).judge(_pairs(1))
    except EntailmentUnavailable:
        pass
    else:
        raise AssertionError("model exception must raise EntailmentUnavailable")


async def test_timeout_raises_unavailable() -> None:
    model = _FakeModel([_reply((0, "supported", "有依据"))], delay=1.0)
    try:
        await SmallLLMEntailment(model, timeout=0.05).judge(_pairs(1))
    except EntailmentUnavailable:
        pass
    else:
        raise AssertionError("timeout must raise EntailmentUnavailable")


async def test_empty_batch_makes_no_call() -> None:
    model = _FakeModel([])
    out = await SmallLLMEntailment(model).judge([])
    assert out == []
    assert model.calls == []


async def test_oversized_batch_is_split_into_ordered_chunks() -> None:
    """25 个 pair、批上限 10 -> 3 次串行调用，判定按原顺序拼回。"""
    replies = [
        _reply(*[(i, "supported", "") for i in range(10)]),
        _reply(*[(i, "uncertain", "") for i in range(10)]),
        _reply(*[(i, "contradicted", "") for i in range(5)]),
    ]
    model = _FakeModel(replies)
    out = await SmallLLMEntailment(model).judge(_pairs(25))
    assert len(model.calls) == 3
    assert [len(chunk) for chunk in model.calls] == [10, 10, 5]
    assert [j.verdict for j in out] == ["supported"] * 10 + ["uncertain"] * 10 + [
        "contradicted"
    ] * 5
    assert [j.citation_id for j in out] == list(range(1, 26))
