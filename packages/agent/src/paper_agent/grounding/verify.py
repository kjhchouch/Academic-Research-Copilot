"""verify_answer：claim 抽取 -> span 绑定 -> L0 查表 -> L1 蕴含 -> 聚合。

唯一入口。分层在签名上就看得见：`backend=None` 不是「什么都不做」，而是退到只剩
确定性层——unbacked 查表、span 缺失标 uncertain，这两个信号不需要模型，也因此
`GROUNDING_BACKEND=off` 仍然兑现一半 README 承诺。

fail-open 的全部方向都在这里收口：backend 抛任何异常（`EntailmentUnavailable`
或别的），进过模型的 pair 一律 `uncertain` / `entailment_unavailable`。一次核验
中断是「无法核验」，绝不是「这引用是假的」。

聚合取**最差**：同一篇文献支撑了 A 句、反驳了 B 句时，chip 不能显示绿。
排序 contradicted < unbacked < uncertain < supported——前两者都是致命信号，
先后只影响日志观感，不影响任何分支。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from paper_agent.citations import CitationRegistry
from paper_agent.grounding.claims import CitedClaim, cited_claims
from paper_agent.grounding.entailment import (
    ClaimSpan,
    EntailmentBackend,
    EntailmentUnavailable,
    Judgement,
)

logger = logging.getLogger(__name__)

# 值越小越糟；聚合取 min。键集合必须等于 VERDICTS，test_verdicts_and_rank_stay_in_sync 钉着。
_RANK: dict[str, int] = {"contradicted": 0, "unbacked": 1, "uncertain": 2, "supported": 3}

SPAN_UNAVAILABLE = "span_unavailable"
ENTAILMENT_UNAVAILABLE = "entailment_unavailable"


def build_pairs(
    claims: Sequence[CitedClaim], abstracts: Mapping[int, str | None]
) -> tuple[list[ClaimSpan], list[int], list[int]]:
    """claims + {citation_id: abstract|None} -> (可核验 pairs, 缺 span 的 id, unbacked 的 id)。

    纯函数。unbacked = 标记在 registry 里没有条目（模型编造的引用号）——它连
    span 都没有，比「span 里找不到依据」严重一档，所以单独一路，不进模型。
    """
    pairs: list[ClaimSpan] = []
    spanless: list[int] = []
    unbacked: list[int] = []
    for claim in claims:
        abstract = abstracts.get(claim.citation_id)
        if claim.citation_id not in abstracts:
            unbacked.append(claim.citation_id)
        elif abstract:
            pairs.append(
                ClaimSpan(claim=claim.text, citation_id=claim.citation_id, span=abstract.strip())
            )
        else:
            spanless.append(claim.citation_id)
    return pairs, spanless, unbacked


async def verify_answer(
    answer: str, registry: CitationRegistry | None, backend: EntailmentBackend | None
) -> list[Judgement]:
    """答案里每个被引用的 citation_id 得到一条最差结论，按 id 升序。

    registry 为 None 意味着「不在一次聊天轮里」（测试直调、非聊天图），citations
    是流的增强不是前提——返回空，绝不把没有 registry 误判成「全是编造的」。
    """
    if registry is None:
        return []
    claims = cited_claims(answer)
    if not claims:
        return []
    abstracts: dict[int, str | None] = {
        citation.citation_id: citation.abstract for citation in registry.citations
    }
    pairs, spanless, unbacked = build_pairs(claims, abstracts)

    judgements: list[Judgement] = [
        Judgement(citation_id=cid, verdict="unbacked") for cid in unbacked
    ]
    judgements.extend(
        Judgement(citation_id=cid, verdict="uncertain", reason=SPAN_UNAVAILABLE) for cid in spanless
    )

    if pairs and backend is not None:
        try:
            judgements.extend(await backend.judge(pairs))
        except EntailmentUnavailable:
            judgements.extend(_unavailable(pairs))
        except Exception:  # noqa: BLE001 - 后端的其他异常同走 fail-open，且要留日志
            logger.exception("grounding backend raised unexpectedly; failing open")
            judgements.extend(_unavailable(pairs))

    return _aggregate_worst(judgements)


def _unavailable(pairs: Sequence[ClaimSpan]) -> list[Judgement]:
    return [
        Judgement(citation_id=pair.citation_id, verdict="uncertain", reason=ENTAILMENT_UNAVAILABLE)
        for pair in pairs
    ]


def _aggregate_worst(judgements: Sequence[Judgement]) -> list[Judgement]:
    """同一 citation_id 取最差（_RANK 最小者），输出按 id 升序。"""
    worst: dict[int, Judgement] = {}
    for judgement in judgements:
        kept = worst.get(judgement.citation_id)
        if kept is None or _RANK[judgement.verdict] < _RANK[kept.verdict]:
            worst[judgement.citation_id] = judgement
    return [worst[cid] for cid in sorted(worst)]
