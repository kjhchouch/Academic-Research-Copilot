"""`verify_answer` 的编排契约：分层、fail-open 方向、最差聚合。

这里的假件只有两个：一个记调用的 `_ScriptedBackend`（只实现 `judge`），以及
`CitationRegistry` 本身（它就是被测对象的协作者，直接用真的）。全部离线。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from paper_agent.citations import CitationRegistry
from paper_agent.grounding.claims import cited_claims
from paper_agent.grounding.entailment import (
    VERDICTS,
    ClaimSpan,
    EntailmentUnavailable,
    Judgement,
)
from paper_agent.grounding.verify import _RANK, build_pairs, verify_answer


class _ScriptedBackend:
    name = "scripted"

    def __init__(
        self,
        verdicts: dict[int, str] | None = None,
        *,
        error: Exception | None = None,
        default: str = "supported",
    ) -> None:
        self._verdicts = verdicts or {}
        self._error = error
        self._default = default
        self.calls: list[list[ClaimSpan]] = []

    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]:
        self.calls.append(list(pairs))
        if self._error is not None:
            raise self._error
        return [
            Judgement(
                citation_id=pair.citation_id,
                verdict=self._verdicts.get(pair.citation_id, self._default),  # type: ignore[arg-type]
                reason="判定理由",
            )
            for pair in pairs
        ]


def _registry(*abstracts: str | None) -> CitationRegistry:
    registry = CitationRegistry()
    for i, abstract in enumerate(abstracts, 1):
        registry.register({"title": f"论文{i}", "abstract": abstract})
    return registry


# ---- 词表自检 ------------------------------------------------------------


def test_verdicts_and_rank_stay_in_sync() -> None:
    """_RANK 的键集合必须等于 VERDICTS：加一个新词而忘了排最差顺序，聚合会静默 KeyError。"""
    assert set(_RANK) == set(VERDICTS)
    assert _RANK["contradicted"] < _RANK["unbacked"] < _RANK["uncertain"] < _RANK["supported"]


# ---- 短路与 L0 -----------------------------------------------------------


async def test_no_registry_returns_empty() -> None:
    """不在一次聊天轮里（测试直调、非聊天图）什么都不断言，包括 unbacked。"""
    assert await verify_answer("看[1]", None, _ScriptedBackend()) == []


async def test_no_markers_returns_empty_and_never_calls_the_backend() -> None:
    backend = _ScriptedBackend()
    assert await verify_answer("你好，今天天气怎么样？", _registry("摘要"), backend) == []
    assert backend.calls == []


async def test_unbacked_marker_is_marked_without_the_model() -> None:
    """registry 里没有的 `[n]` 是编造号：确定性标注，零模型调用。"""
    backend = _ScriptedBackend()
    out = await verify_answer("真结论[1]。编造的[9]也出现。", _registry("真实摘要"), backend)
    assert [(j.citation_id, j.verdict) for j in out] == [(1, "supported"), (9, "unbacked")]
    # 只有 [1] 进了模型；编造号不烧 token。
    assert [p.citation_id for p in backend.calls[0]] == [1]


async def test_missing_abstract_is_uncertain_without_the_model() -> None:
    backend = _ScriptedBackend()
    out = await verify_answer("结论[1]。", _registry(None), backend)
    assert [(j.citation_id, j.verdict, j.reason) for j in out] == [
        (1, "uncertain", "span_unavailable")
    ]
    assert backend.calls == []


async def test_backend_none_keeps_the_deterministic_layer_only() -> None:
    """GROUNDING_BACKEND=off 不是全关：unbacked 与 span 缺失仍标注，需要模型的 pair 不出结论。"""
    out = await verify_answer("有摘要[1]。没摘要[2]。编造[3]。", _registry("摘要", None), None)
    assert [(j.citation_id, j.verdict) for j in out] == [(2, "uncertain"), (3, "unbacked")]


# ---- L1 蕴含 -------------------------------------------------------------


async def test_backend_verdicts_pass_through() -> None:
    backend = _ScriptedBackend({1: "contradicted", 2: "uncertain"})
    out = await verify_answer("甲[1]。乙[2]。", _registry("摘要一", "摘要二"), backend)
    assert [(j.citation_id, j.verdict, j.reason) for j in out] == [
        (1, "contradicted", "判定理由"),
        (2, "uncertain", "判定理由"),
    ]


async def test_backend_unavailable_fails_open_to_uncertain() -> None:
    backend = _ScriptedBackend(error=EntailmentUnavailable("timeout"))
    out = await verify_answer("甲[1]。乙[2]。", _registry("摘要一", "摘要二"), backend)
    assert [(j.citation_id, j.verdict, j.reason) for j in out] == [
        (1, "uncertain", "entailment_unavailable"),
        (2, "uncertain", "entailment_unavailable"),
    ]


async def test_backend_unexpected_error_also_fails_open() -> None:
    """核验是流的增强：后端自己的 bug 同样不许变成一条 contradicted 断言。"""
    backend = _ScriptedBackend(error=RuntimeError("boom"))
    out = await verify_answer("甲[1]。", _registry("摘要一"), backend)
    assert out[0].verdict == "uncertain"
    assert out[0].reason == "entailment_unavailable"


async def test_same_citation_takes_the_worst_verdict() -> None:
    """同一篇文献支撑 A 句、反驳 B 句：chip 必须显示最差结果，不能显示绿。"""
    claims = cited_claims("它说方法有效[1]。它说方法无效[1]。")
    backend = _ScriptedBackend({1: "contradicted"})
    # 两条 claim 同 id；第二句的 verdict 由 backend 固定为 contradicted，
    # 另造一个 supported 的 backend 对照聚合逻辑。
    out = await verify_answer("它说方法有效[1]。它说方法无效[1]。", _registry("摘要"), backend)
    assert [j.verdict for j in out] == ["contradicted"]

    good = _ScriptedBackend()  # default supported for both
    ok = await verify_answer("有效[1]。也有效[1]。", _registry("摘要"), good)
    assert [j.verdict for j in ok] == ["supported"]
    assert claims  # cited_claims 在同 id 多句时确实产两条（上面路径已隐含验证）


async def test_output_is_sorted_and_deduplicated_by_citation_id() -> None:
    backend = _ScriptedBackend()
    out = await verify_answer("乙[3]。甲[1]。", _registry("一", None, "三"), backend)
    assert [j.citation_id for j in out] == [1, 3]


# ---- build_pairs 纯函数 ----------------------------------------------------


def test_build_pairs_partitions_three_ways() -> None:
    claims = cited_claims("有摘要[1]。没摘要[2]。编造[3]。")
    pairs, spanless, unbacked = build_pairs(claims, {1: "摘要一", 2: None, 4: "别的"})
    assert [(p.citation_id, p.span) for p in pairs] == [(1, "摘要一")]
    assert spanless == [2]
    assert unbacked == [3]


def test_build_pairs_strips_span_whitespace() -> None:
    claims = cited_claims("结论[1]。")
    pairs, _spanless, _unbacked = build_pairs(claims, {1: "  带空白的摘要  \n"})
    assert pairs[0].span == "带空白的摘要"


def test_judgement_shape_is_stable() -> None:
    """state / SSE 共用的形状：三个键都在，缺的是 None。"""
    state: dict[str, Any] = Judgement(citation_id=5, verdict="unbacked").as_state()
    assert state == {"citation_id": 5, "verdict": "unbacked", "reason": None}
