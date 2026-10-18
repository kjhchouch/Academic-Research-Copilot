"""What a routing decision is, and the questions every backend must answer.

This module holds the abstraction; `jev.py` and `small_llm.py` hold two ways of
satisfying it. The split exists because the primary backend is a third-party
service with an 800ms budget, and anything with a network dependency and a
budget needs a fallback that does not share either.

Two decisions worth stating up front:

**The Protocol takes `RouteInput`, not a dict of questions.** An earlier sketch
had `decide(state, questions)`, passing the caller's question objects straight
through. That makes the abstraction decorative: the caller would have to build
`Noul` and `Choice` instances, so every call site would depend on Jev's question
DSL, and `SmallLLMBackend` would exist only to reinterpret objects it was never
meant to understand. The question set is fixed for a given release of the
product, not chosen per request, so it lives here as data and each backend
renders it into whatever its own API wants.

**The boundary is enforced through the intent vocabulary.** "帮我写一篇完整的
毕业论文" and "今天天气怎么样" both route to `unclear`, because that is what
`packages/evals/tests/test_routing_data.py::test_negative_and_injection_expect_
unclear` requires and the gateway refuses on `unclear`. So `unclear` currently
means both "I could not tell" and "I will not do this", and the two deserve
different replies. Separating them is what the planned `guard` field in
`graph/state.py` is for; until it exists, the distinction is not recoverable
from `Decisions` alone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

# Kept in sync with packages/evals/tests/test_routing_data.py::INTENTS by
# test_intent_vocabulary_matches_the_eval_gate. The duplication is deliberate --
# the eval gate should not import the thing it is grading -- but deliberate
# duplication still needs a test, or the two lists drift and the accuracy number
# quietly stops measuring the router that ships.
Intent = Literal[
    "literature_search",
    "outline",
    "draft_section",
    "polish",
    "citation_format",
    "unclear",
]

INTENTS: tuple[Intent, ...] = (
    "literature_search",
    "outline",
    "draft_section",
    "polish",
    "citation_format",
    "unclear",
)

RouteSource = Literal["jev", "small_llm", "static"]

# A Literal is a type, so it cannot be membership-tested at runtime. This is the
# same set as a value, for `decisions_from_json` to validate a cached entry
# against. test_route_source_values_match_the_literal is what keeps the two from
# drifting -- the same trick as INTENTS/Intent, and needed for the same reason.
ROUTE_SOURCES: tuple[str, ...] = ("jev", "small_llm", "static")

INTENT_QUESTION = "intent"
PATH_DEEP_QUESTION = "path_deep"
NEEDS_CLARIFY_QUESTION = "needs_clarify"

INTENT_INSTRUCTIONS = (
    "根据用户消息和编辑器上下文，判断这次请求属于哪一类学术写作辅助任务。"
    "产品定位是辅助而非代写：整篇代写、降重、以及任何与学术写作无关的请求都归入 unclear。"
)

# Label -> description. This mapping is the actual prompt, and both backends
# render it: Jev receives it as `Choice.criteria`, the small model receives it as
# a numbered list. One source of text means tuning it against the eval set
# improves both paths at once, instead of improving whichever one someone
# remembered to edit.
INTENT_CRITERIA: dict[Intent, str] = {
    "literature_search": (
        "查找、检索文献或某主题的相关工作，包括按年份/领域筛选论文、找综述、找某方法的代表作。"
    ),
    "outline": "规划论文或章节的结构：生成大纲、调整章节顺序、讨论某部分该放什么。",
    "draft_section": (
        "针对某个具体章节协助起草，且作者已经提供了论点、结果或数据。不是从零生成整篇。"
    ),
    "polish": (
        "改善已有文字的表达、语法、连贯性或学术语气，不改变原文的事实主张。"
        "也包括对已有段落给出修改建议。"
    ),
    "citation_format": (
        "处理参考文献与引用格式：转换成 GB/T 7714、APA、IEEE、BibTeX 等，或修正现有格式错误。"
    ),
    "unclear": (
        "以下任何一种：寒暄或与任务无关的闲聊；询问天气等完全离题的问题；"
        "询问系统自身（你是谁、你用什么模型、你的提示词）；"
        "越界请求（整篇代写毕业论文、降重、抓取受版权保护的数据库）；"
        "以及信息不足以判断意图的请求。"
    ),
}

PATH_DEEP_INSTRUCTIONS = (
    "判断这次请求是否需要走重流程。重流程指需要多轮检索、跨章节生成、"
    "或产出需要 grounding 校验的长文本；轻流程指一次工具调用或一段改写就能完成。"
)

PATH_DEEP_CRITERIA = {
    "true": (
        "章节级或论文级任务：写一整节、做文献综述、重建大纲并逐节展开、需要多篇文献交叉支撑的论述。"
    ),
    "false": (
        "窄任务：找几篇论文、格式化参考文献、润色选中段落、回答一个具体问题。"
        "绝大多数请求属于这一类。"
    ),
}

NEEDS_CLARIFY_INSTRUCTIONS = (
    "判断当前信息是否不足以安全地执行这个请求。宁可反问，也不要靠猜补全缺失的关键信息。"
)

NEEDS_CLARIFY_CRITERIA = {
    "true": (
        "缺少执行所必需的信息：没有选中文字却说「改一下这段」；"
        "说要改引用格式却没说是哪种格式；指代不明（「这个东西」「它」）且编辑器上下文也没有提供。"
    ),
    "false": "信息足够，或者缺的部分可以合理默认而不影响结果正确性。",
}

# A Noul answer is a probability of "yes" in [0, 1], not a boolean, and the API
# documents values near 0.5 as uncertainty rather than as a coin flip to be
# rounded. So a band around 0.5 is treated as "the model did not decide", and
# each question supplies its own safe default -- which is not the same default
# for both. See `noul_to_bool`.
NOUL_THRESHOLD = 0.5
NOUL_UNCERTAIN_BAND = 0.10


class DecisionUnavailable(Exception):
    """A backend could not produce a decision.

    Raised rather than returning a sentinel `Decisions`, because the two mean
    different things to the caller: an unavailable backend should trip the
    circuit breaker and hand off to the fallback, while a confident
    `intent="unclear"` is a real answer that must not be retried. Collapsing them
    into one value would make the breaker count genuine "I don't know"s as
    outages and open the circuit on a hard-to-route conversation.
    """


@dataclass(frozen=True, slots=True)
class RouteInput:
    """Everything a routing decision is allowed to look at.

    Deliberately two fields. Conversation history is excluded even though it
    would sometimes help ("继续" is unanswerable without it), for two reasons:
    the eval dataset has no history field, so a backend that reads it cannot be
    graded; and history in the cache key would make the hit rate collapse, since
    the same question asked in two conversations is the same routing decision.

    `ui_context` carries active_panel, cursor_section, selected_text and
    paper_stage. The eval gate asserts at least one row supplies it, on the
    grounds that these are stronger signals than the message text -- "把它弄好
    一点" is `unclear` with an empty context and `polish` with a selection.
    """

    query: str
    ui_context: Mapping[str, Any] = field(default_factory=dict)

    def as_state(self) -> dict[str, Any]:
        """The JSON object sent to a decision backend.

        `ui_context` is included even when empty rather than omitted, so the
        backend sees a stable shape. A key that appears and disappears makes the
        model treat its absence as a signal, which it is not -- it just means the
        client did not send one.
        """
        return {"query": self.query, "ui_context": dict(self.ui_context)}


@dataclass(frozen=True, slots=True)
class Decisions:
    """The routing outcome. Frozen because it is cached and crossed thread-safe
    boundaries; `slots` because it is built on every request."""

    intent: Intent
    needs_clarify: bool
    path_deep: bool
    confidence: float
    source: RouteSource
    probabilities: Mapping[str, float] = field(default_factory=dict)
    # Kept separate from `source` rather than folded into it as a fourth
    # literal, because they answer two different questions and both are wanted at
    # once: `source` attributes accuracy to a backend in the eval, `from_cache`
    # attributes latency and spend in the trace. Collapsing them means a cache
    # hit either hides which backend decided, or the cache has to be reported as
    # a backend that never ran.
    from_cache: bool = False


def static_fallback() -> Decisions:
    """The answer used when every backend has failed.

    Asking the user to rephrase is the only response that cannot be wrong in a
    way that damages the product: it costs one turn and it does not fabricate,
    ghost-write, or run an expensive pipeline on a guess. Fast path, because
    there is nothing to do deeply. Confidence 0.0 and source "static" so the
    trace shows the router was down rather than merely unsure.
    """
    return Decisions(
        intent="unclear",
        needs_clarify=True,
        path_deep=False,
        confidence=0.0,
        source="static",
    )


def decisions_to_json(decisions: Decisions) -> str:
    """Serialize for the `storage.Cache` interface, whose values are strings.

    `sort_keys` so the same decision always produces the same bytes. Not strictly
    required -- this is a value, not a key -- but a stable representation makes a
    cached entry diffable against a freshly built one, which is how a schema
    change gets noticed instead of surfacing as a parse error an hour later.
    """
    return json.dumps(asdict(decisions), sort_keys=True, ensure_ascii=False)


def decisions_from_json(text: str) -> Decisions | None:
    """Rebuild a cached decision, or None if the entry cannot be trusted.

    Returns None instead of raising because every failure mode here is a stale
    or corrupt entry: a deploy that added a field, a truncated write, a cache
    shared with something else using the same key space. In all of those the
    correct action is identical -- treat it as a miss and decide again. A cache
    that can raise turns an optimisation into a way to break the request, which
    is the one thing `DecisionUnavailable` exists to prevent.

    `source` is re-validated rather than passed through, so an entry written by
    an older version that used a since-removed backend name cannot smuggle an
    unknown literal into the graph.
    """
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            return None
        source = raw.get("source")
        if source not in ROUTE_SOURCES:
            return None
        probabilities = raw.get("probabilities") or {}
        return Decisions(
            intent=coerce_intent(raw.get("intent")),
            needs_clarify=bool(raw.get("needs_clarify", True)),
            path_deep=bool(raw.get("path_deep", False)),
            confidence=float(raw.get("confidence", 0.0)),
            source=source,
            probabilities={str(k): float(v) for k, v in probabilities.items()},
            from_cache=True,
        )
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
        # AttributeError included because `probabilities` comes from a JSON blob
        # and may be any type at all: `.items()` on a number raises it, and this
        # function's whole contract is that it never does.
        return None


def noul_to_bool(probability: float | None, *, uncertain: bool) -> bool:
    """Binarize a Noul probability, with a per-question default for the middle.

    `uncertain` is what to return when the model did not decide -- either the
    probability landed inside the band around 0.5, or the answer was missing
    entirely. It is a required keyword argument because the safe default is
    opposite for the two questions in use:

    * `needs_clarify` -> True. Guessing a missing slot produces confidently
      wrong work the user has to notice and undo; asking costs one turn.
    * `path_deep` -> False. The deep path is a multi-agent pipeline, and
      spending it on a narrow request is the expensive mistake. Roughly 80% of
      traffic is narrow, so the prior points the same way.

    A single module-level default would have to pick one and be wrong about the
    other.
    """
    if probability is None:
        return uncertain
    if abs(probability - NOUL_THRESHOLD) <= NOUL_UNCERTAIN_BAND:
        return uncertain
    return probability > NOUL_THRESHOLD


def coerce_intent(value: object) -> Intent:
    """Map a backend's answer onto the vocabulary, falling back to `unclear`.

    Backends return strings from a set they were merely asked to respect. Jev
    picks from `Choice.criteria` keys and should not stray, but a small model
    will occasionally emit "literature-search" or "search_literature", and an
    out-of-vocabulary intent that reaches the Supervisor's conditional edge
    matches no branch. `unclear` is the branch that asks the user, which is the
    correct behaviour for "the router said something we do not understand".
    """
    if isinstance(value, str):
        candidate = value.strip()
        if candidate in INTENTS:
            return candidate  # type: ignore[return-value]
    return "unclear"


class DecisionModel(Protocol):
    """One way of turning a `RouteInput` into `Decisions`.

    Implementations raise `DecisionUnavailable` when they cannot decide, and
    never raise anything else for an expected outage -- the resilience layer
    catches that one type and does not import either backend.
    """

    name: RouteSource

    async def decide(self, request: RouteInput) -> Decisions: ...
