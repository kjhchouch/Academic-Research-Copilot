"""蕴含后端：一对 (claim, span) 是否支撑，以及降级档实现。

抽象与 `routing/decision_model.py` 同源：Protocol 在这，具体后端在旁边。区别在于
grounding 的调用天然是**批量**的——一条答案里所有被引用的 claim 一次判完，逐 pair
一次调用会把 5 个引用变成 5 次往返，成本和延迟都不成立。

`SmallLLMEntailment` 与 `routing/small_llm.py` 完全同构，理由也一样：裸 JSON 手工解析
而不是 `with_structured_output`，保住两个性质——降级档对模型零能力要求（能吐字即可），
以及 `ScriptedChatModel` 式的假件能离线驱动它（bind_tools 的假件问题在 D3 已经付过学费）。

失败语义分两档，这是本模块最重要的契约：

* **结构失配 -> `EntailmentUnavailable`**。JSON 解析不了、项数不对、index 对不上——
  此时无法知道哪个 pair 对应哪条判定，任何部分采纳都是「半可信的结果」这种没人能
  消费的状态。整批抛出让调用方 fail-open。
* **值级失配 -> coerce 成 `uncertain`**。pair 已定位，只是模型给了认不出的标签
  （包括它不该产的 `unbacked`——那是 L0 查表专属）。这一对诚实标签就是 uncertain，
  不该拖累同批其余 pair。

`unbacked` 不在 `MODEL_VERDICTS` 里不是遗漏：它是 CitationRegistry 查表的确定性产物，
模型说 unbacked 意味着模型在猜它没见过的注册表，猜的内容一律不可信。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

Verdict = Literal["supported", "uncertain", "contradicted", "unbacked"]

# 四值词表的全集。unbacked 只能由 L0（verify.py 查 registry）产出。
VERDICTS: tuple[str, ...] = ("supported", "uncertain", "contradicted", "unbacked")

# 蕴含后端允许产出的子集。unbacked 是确定性层专属，见模块 docstring。
MODEL_VERDICTS: tuple[str, ...] = ("supported", "uncertain", "contradicted")

# reason 会进 SSE 事件和前端悬浮提示。截断不是为了省带宽，是防止一条跑偏的
# reason 把悬浮框撑到盖住正文。
MAX_REASON_CHARS = 200

# 每批 pair 数上限。超过就分批串行：一次调用塞 30 个判例，小模型会开始漏项，
# 而漏项在本模块的契约里是整批 unavailable——批越大，一次失配浪费的越多。
BATCH_SIZE = 10

# 比 routing 的 small_llm 路由宽一倍：这个调用发生在用户已经在读答案之后，
# 多等两秒换一个更低的不确定率是值的；但仍要有界，否则一次挂起的调用把
# 「已经答完的流」拖住一分钟。
SMALL_LLM_ENTAILMENT_TIMEOUT_SECONDS = 8.0

SYSTEM_PROMPT = """你是引用核验器。逐条判断每条「主张」是否被对应的「来源文本」支持，\
输出一个 JSON 数组，不要输出任何别的文字。

每项格式：
{"index": <对应输入的行号>, "verdict": "supported|uncertain|contradicted", "reason": "<一句话理由>"}

判定标准：
- supported：来源文本明确陈述了主张的内容，同义改写也算支持。
- contradicted：来源文本明确陈述了与主张相反的内容。
- uncertain：来源文本没有提到、信息不足以判断、或你不确定。

主张中的数字、人名、方法名和结论必须能在来源文本中找到依据才算 supported。\
宁可 uncertain，也不要猜。"""


class EntailmentUnavailable(Exception):
    """后端无法产出可信判定（结构失配 / 超时 / 模型异常）。

    调用方（verify.py）捕获后把整批标 uncertain——核验服务不可用必须表现为
    「无法核验」，绝不能表现为任何一种断言。
    """


@dataclass(frozen=True, slots=True)
class ClaimSpan:
    """一个待核验单元：claim 文本、它指向的引用号、被核验所依据的 span 文本。"""

    claim: str
    citation_id: int
    span: str


@dataclass(frozen=True, slots=True)
class Judgement:
    """一条引用的核验结论。reason 给人看（悬浮提示），给不出就 None。"""

    citation_id: int
    verdict: Verdict
    reason: str | None = None

    def as_state(self) -> dict[str, Any]:
        """写入图 state / SSE 事件的稳定形状：每个键都在，缺的是 None。"""
        return {"citation_id": self.citation_id, "verdict": self.verdict, "reason": self.reason}


class EntailmentBackend(Protocol):
    """一个蕴含后端。失败只抛 `EntailmentUnavailable`，其余异常视为编程错误。"""

    name: str

    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]: ...


class L0OnlyBackend:
    """`GROUNDING_BACKEND=off` 的载体：不跑模型蕴含，只保留确定性层。

    build_graph 的契约是 `entailment=None` -> 根本不接 verify 节点（D9 之前的图
    必须逐字节不变），所以「off 但仍要 L0 标注」需要的是一个对象而不是 None。
    `judge` 永远返回空：pair 没有模型可问，就没有判定（不出现在输出里）；
    unbacked 查表与 span 缺失标注仍由 verify_answer 的确定性路径完成。

    `name` 刻意是 None：SSE 事件的 `source` 因此为 null——这一轮的标注全部来自
    确定性层，出处必须可辨。
    """

    name: str | None = None

    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]:
        return []


def _text_of(content: Any) -> str:
    """Flatten message content, which is a str or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


def _extract_array(text: str) -> list[Any]:
    """从可能带围栏和前言的回复里取出 JSON 数组。"""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise EntailmentUnavailable("small_llm entailment: no JSON array in reply")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise EntailmentUnavailable(f"small_llm entailment: malformed JSON ({exc.msg})") from exc
    if not isinstance(parsed, list):
        raise EntailmentUnavailable("small_llm entailment: reply is not an array")
    return parsed


def _coerce_verdict(value: Any) -> Verdict:
    """认得的标签原样收下，认不得的一律 uncertain（含 unbacked，见模块 docstring）。"""
    if isinstance(value, str) and value.strip().lower() in MODEL_VERDICTS:
        return value.strip().lower()  # type: ignore[return-value]
    return "uncertain"


def _clamp_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    reason = value.strip()
    return reason[:MAX_REASON_CHARS]


class SmallLLMEntailment:
    """`EntailmentBackend` backed by any LangChain chat model（含测试假件）。

    `name` 进 SSE 事件的 `source` 字段和评测的 `by_backend`，让标注结果永远带着
    「谁判的」——和路由的 `by_source` 同一条纪律：降级档的数字不能被当成生产档的。
    """

    name = "small_llm"

    def __init__(
        self,
        model: Any,
        *,
        timeout: float = SMALL_LLM_ENTAILMENT_TIMEOUT_SECONDS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._batch_size = max(1, batch_size)

    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]:
        """判完所有 pair；任何失败抛 `EntailmentUnavailable`，绝不返回半批结果。"""
        if not pairs:
            return []
        judgements: list[Judgement] = []
        for start in range(0, len(pairs), self._batch_size):
            chunk = list(pairs[start : start + self._batch_size])
            judgements.extend(await self._judge_chunk(chunk))
        return judgements

    async def _judge_chunk(self, chunk: list[ClaimSpan]) -> list[Judgement]:
        payload = [
            {"index": i, "claim": pair.claim, "source": pair.span} for i, pair in enumerate(chunk)
        ]
        messages: list[BaseMessage] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            response = await asyncio.wait_for(self._model.ainvoke(messages), timeout=self._timeout)
        except TimeoutError as exc:
            logger.warning("small_llm entailment exceeded %.1fs", self._timeout)
            raise EntailmentUnavailable("small_llm entailment: timeout") from exc
        except Exception as exc:  # noqa: BLE001 - entailment must never break the stream
            logger.warning("small_llm entailment failed: %s: %s", type(exc).__name__, exc)
            raise EntailmentUnavailable(f"small_llm entailment: {type(exc).__name__}") from exc

        items = _extract_array(_text_of(getattr(response, "content", "")))
        if len(items) != len(chunk):
            raise EntailmentUnavailable(
                f"small_llm entailment: expected {len(chunk)} items, got {len(items)}"
            )
        judgements: list[Judgement] = []
        for i, (pair, item) in enumerate(zip(chunk, items, strict=True)):
            if not isinstance(item, dict) or item.get("index") != i:
                raise EntailmentUnavailable("small_llm entailment: item index mismatch")
            raw = item.get("verdict")
            if raw not in MODEL_VERDICTS:
                # 认不出的标签已 coerce 成 uncertain，但要在日志里留痕——静默吞掉
                # 等于词表漂移没人知道（unbacked 出现在这里同样算漂移：模型不该产它）。
                logger.warning(
                    "small_llm entailment: unrecognized verdict %r for citation %s",
                    raw,
                    pair.citation_id,
                )
            judgements.append(
                Judgement(
                    citation_id=pair.citation_id,
                    verdict=_coerce_verdict(raw),
                    reason=_clamp_reason(item.get("reason")),
                )
            )
        return judgements
