"""Grounding：Fast Path 答案的引用级标注层。

README 承诺「未获支持的引用会被标记」。D6 落地的是编号层（citation_id 由网关
分配、模型抄号），本包补的是支撑性标注：答案里的每个 `[n]` 得到四值之一——

    supported     span（引用论文的摘要）明确支撑这句 claim
    uncertain     span 未提及 / 信息不足 / 任何基础设施失败（fail-open 方向）
    contradicted  span 明确陈述了相反内容（只有模型能产出）
    unbacked      `[n]` 在 CitationRegistry 里没有对应条目（只有 L0 能产出）

分层与方向的两条原则，与 routing 同源，论证见 specs/2026-10-31：

* **能确定性的绝不概率化**——unbacked 是查表，零模型调用，`backend=None` 时也标注。
* **fail-open 永远指向 uncertain**——核验服务超时/解析失败表现为「无法核验」，
  绝不表现为「这引用是假的」。contradicted 是一个断言，断言需要证据。

这是**标注层**不是**拦截层**：低置信引用被标给用户看，不删除、不重试（重试属于
Deep Path 的 Verifier→Drafter 回退，是另一张图）。
"""

from paper_agent.grounding.claims import CitedClaim, cited_claims
from paper_agent.grounding.entailment import (
    MODEL_VERDICTS,
    VERDICTS,
    ClaimSpan,
    EntailmentBackend,
    EntailmentUnavailable,
    Judgement,
    L0OnlyBackend,
    SmallLLMEntailment,
)
from paper_agent.grounding.verify import verify_answer

__all__ = [
    "MODEL_VERDICTS",
    "VERDICTS",
    "CitedClaim",
    "ClaimSpan",
    "EntailmentBackend",
    "EntailmentUnavailable",
    "Judgement",
    "L0OnlyBackend",
    "SmallLLMEntailment",
    "cited_claims",
    "verify_answer",
]
