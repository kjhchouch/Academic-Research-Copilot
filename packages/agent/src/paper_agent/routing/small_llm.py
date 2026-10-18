"""The fallback routing backend: one small chat model call, parsed as JSON.

This is what runs when Jev is unconfigured, over budget, or tripping the circuit
breaker. It has to be worse than the primary and still correct, because the
fast path keeps working while it is in use -- a degradation that changes what
the product does is not a degradation, it is an outage with extra steps.

Two things it gives up relative to Jev, both worth knowing when reading an eval:

* **No calibrated confidence.** The model is asked to self-report a number, and
  self-reported confidence from a chat model is not a probability. `confidence`
  is therefore only comparable *within* one source; a 0.6 from here and a 0.6
  from Jev do not mean the same thing. Nothing thresholds on it.
* **No probability distribution.** `probabilities` stays empty, so the eval
  cannot show which intent was the runner-up.

Plain JSON rather than `with_structured_output` on purpose. Structured output
routes through tool calling or a JSON-schema mode, which binds this backend to
models that implement it and -- more immediately -- to a test double that does
not: `ScriptedChatModel.bind_tools` is a no-op that returns the script
unchanged. Parsing by hand is a few lines and means the fallback has no
capability requirement beyond "can emit text", which is the right property for
the thing that runs when everything else has failed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from paper_agent.routing.decision_model import (
    INTENT_CRITERIA,
    INTENTS,
    NEEDS_CLARIFY_CRITERIA,
    PATH_DEEP_CRITERIA,
    Decisions,
    DecisionUnavailable,
    RouteInput,
    RouteSource,
    coerce_intent,
)

logger = logging.getLogger(__name__)

# Longer than Jev's 800ms because this is already the degraded path: there is
# nothing left to fall back to except the static "please rephrase", so trading
# some latency for a real answer is the right trade. Still bounded, because
# `make_chat_model` defaults to a 60s socket timeout and a router that stalls
# for a minute has turned a routing outage into a hung request.
SMALL_LLM_TIMEOUT_SECONDS = 4.0


def _render_criteria(criteria: dict[str, str]) -> str:
    return "\n".join(f"- {label}: {text}" for label, text in criteria.items())


SYSTEM_PROMPT = f"""你是一个意图分类器。
根据用户消息和编辑器上下文，输出一个 JSON 对象，不要输出任何别的文字。

可选的 intent（必须严格从中选一个）：
{_render_criteria(dict(INTENT_CRITERIA))}

同时判断两个布尔值：

path_deep —— 是否需要走重流程（多轮检索、跨章节生成、需要 grounding 校验的长文本）：
{_render_criteria(PATH_DEEP_CRITERIA)}

needs_clarify —— 当前信息是否不足以安全地执行这个请求：
{_render_criteria(NEEDS_CLARIFY_CRITERIA)}

输出格式：
{{
  "intent": "<上面六个之一>",
  "path_deep": <true|false>,
  "needs_clarify": <true|false>,
  "confidence": <0到1之间的小数>
}}

信息不足时宁可 needs_clarify 为 true，也不要靠猜补全。
越界请求（整篇代写、降重）和离题请求一律 intent 为 unclear。"""

_VALID_INTENT_LIST = ", ".join(INTENTS)


def _as_bool(value: Any, *, uncertain: bool) -> bool:
    """Coerce a model's idea of a boolean.

    Asked for `true`, a small model returns `true`, `"true"`, `"yes"`, `1`, or
    occasionally `"是"`. Anything unrecognisable takes the same per-question safe
    default `noul_to_bool` uses for an undecided probability, so a parse miss
    costs the user a clarifying question rather than an expensive pipeline run.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "1", "是"}:
            return True
        if text in {"false", "no", "n", "0", "否"}:
            return False
    return uncertain


def _as_confidence(value: Any) -> float:
    """Read a self-reported confidence, or 0.0 if it is absent or malformed.

    0.0 here means "not reported", not "certainly wrong" -- the intent may be
    perfectly good. Callers must not threshold on this value without knowing the
    source; see the module docstring.
    """
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(confidence, 0.0), 1.0)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the object out of a model reply that may have decorated it.

    Models wrap JSON in ```json fences and prepend "好的，以下是结果：" even when
    told not to, so the first `{` to the last `}` is taken rather than the whole
    string. That is a heuristic and it is enough: a reply with two separate JSON
    objects is not something this prompt produces, and if it ever does, the parse
    fails and the router degrades to asking the user.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise DecisionUnavailable("small_llm: no JSON object in reply")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DecisionUnavailable(f"small_llm: malformed JSON ({exc.msg})") from exc
    if not isinstance(parsed, dict):
        raise DecisionUnavailable("small_llm: JSON reply is not an object")
    return parsed


def _text_of(content: Any) -> str:
    """Flatten message content, which is a str or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


class SmallLLMBackend:
    """`DecisionModel` backed by any LangChain chat model.

    Takes a Runnable (anything with `ainvoke`), not a `BaseChatModel`, matching
    `graph.nodes.make_agent_node`: production passes `make_chat_model(...)` and
    tests pass `ScriptedChatModel`.
    """

    name: RouteSource = "small_llm"

    def __init__(self, model: Any, *, timeout: float = SMALL_LLM_TIMEOUT_SECONDS) -> None:
        self._model = model
        self._timeout = timeout

    async def decide(self, request: RouteInput) -> Decisions:
        messages: list[BaseMessage] = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(request.as_state(), ensure_ascii=False)),
        ]

        try:
            # wait_for rather than the model's own timeout: this backend accepts
            # an arbitrary Runnable and cannot assume it honours one, and the
            # bound has to hold for the fallback path to be safe to rely on.
            response = await asyncio.wait_for(self._model.ainvoke(messages), timeout=self._timeout)
        except TimeoutError as exc:
            logger.warning("small_llm routing exceeded %.1fs", self._timeout)
            raise DecisionUnavailable("small_llm: timeout") from exc
        except Exception as exc:  # noqa: BLE001 - routing must never break the request
            logger.warning("small_llm routing failed: %s: %s", type(exc).__name__, exc)
            raise DecisionUnavailable(f"small_llm: {type(exc).__name__}") from exc

        raw = _text_of(getattr(response, "content", ""))
        payload = _extract_json(raw)

        intent = coerce_intent(payload.get("intent"))
        if intent == "unclear" and payload.get("intent") not in (None, "unclear"):
            # Distinguish "the model chose unclear" from "the model invented a
            # label we do not have". Both end up unclear, but the second means
            # the prompt or the vocabulary has drifted, and silently treating it
            # as a legitimate answer is how that drift goes unnoticed.
            logger.warning(
                "small_llm returned out-of-vocabulary intent %r (expected one of: %s)",
                payload.get("intent"),
                _VALID_INTENT_LIST,
            )

        return Decisions(
            intent=intent,
            needs_clarify=_as_bool(payload.get("needs_clarify"), uncertain=True),
            path_deep=_as_bool(payload.get("path_deep"), uncertain=False),
            confidence=_as_confidence(payload.get("confidence")),
            source=self.name,
        )
