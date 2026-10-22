"""L0: the deterministic boundary gate, and the `guard` that reads it.

`decision_model.py` documents the problem this solves. The intent vocabulary has
one bucket, `unclear`, that the eval forces two very different requests into:

* "帮我把这段降重" -- we will not do this, and no amount of rephrasing changes
  that. It is out of the product on principle.
* "在吗" -- we cannot tell what this wants, and a one-line clarification would
  resolve it.

Both route to `unclear`, so `Decisions` alone cannot say which reply is correct.
`guard` is the missing field: it separates "won't" from "couldn't", which is what
lets `refuse` and `clarify` be distinct graph exits (see `graph/state.py`) instead
of one vague "please rephrase".

**Why the boundary is a rule and not a prediction.** The whole point of the
product's positioning -- 辅助不代写 -- is that it holds. A guarantee that depends
on a model correctly classifying "降重" as out-of-scope on every phrasing is not a
guarantee; it is a probability, and the eval's boundary-leak number measures how
often that probability fails. So the integrity boundary is checked here, with
patterns, before the model's opinion is consulted. This is the L0 tier that
`routing/__init__.py` argues should exist: deterministic, free, and it makes the
boundary *enforced* rather than *predicted*.

**L0 overrides the model, and that is the feature.** If the router leaks -- calls
"帮我把这段降重" a `polish` -- `guard_from` still returns `refuse`, because the
rule runs on the raw query regardless of `intent`. The consequence worth stating
plainly: for the phrasings these patterns cover, the guard-level boundary leak is
zero independent of model accuracy. The model can be wrong and the boundary still
holds. That is a stronger claim than any accuracy number, and it is the reason
this is a gate in the graph rather than a hint in the prompt.

**The residual gap is irreducible, and we do not hide it.** Patterns catch the
explicit phrasings. A determined user who says "让这段更像原创" with no keyword in
it is indistinguishable, at the text level, from a legitimate polish of their own
prose -- no classifier, rule- or model-based, separates those reliably. So L0 is
not a claim that misuse is impossible; it is a claim that the *stated* boundary
violations are refused deterministically, and everything ambiguous degrades to
`clarify` (ask), never to `proceed` (act on a guess).
"""

from __future__ import annotations

import re
from typing import Literal

from paper_agent.routing.decision_model import Decisions, RouteInput

# The three-way the graph branches on. `proceed` runs the agent; the other two are
# terminal exits that emit a fixed message and stop -- no model call, so neither
# can be talked into doing the thing it refused.
Guard = Literal["proceed", "clarify", "refuse"]

# A boundary family, for logging and telemetry: knowing *why* a request was
# refused is the difference between a debuggable gate and a black box that says
# "no" for reasons nobody can see.
BoundaryFamily = Literal["evasion", "ghostwriting", "scraping"]

# Each entry is (family, pattern). IGNORECASE because the evasion and scraping
# families carry latin tokens (AI, AIGC, CNKI) that arrive in any casing.
#
# These are deliberately high-precision and narrow. A false `refuse` on a
# legitimate request is a worse failure than a missed one, because the missed
# case still degrades safely -- the model routes it to `unclear` and the user gets
# `clarify`, not a ghostwritten paper. So every pattern here was checked against
# the whole of packages/evals/data/routing.jsonl: it must fire on the integrity
# rows and on nothing else. The row that constrains the ghostwriting family hardest
# is "帮我把第二章整章写出来，数据我已经跑完了" -- a *legitimate* draft_section.
# 整章 (a whole chapter, with the author's own data) is not 整篇 (a whole paper),
# and no pattern below matches it.
_BOUNDARY_RULES: tuple[tuple[BoundaryFamily, re.Pattern[str]], ...] = (
    # --- plagiarism / AI-detection evasion -------------------------------- #
    # "降重" is the canonical term. The rest are the phrasings that mean the same
    # thing without saying it: lowering the similarity ratio, or stripping the
    # traces a detector keys on.
    ("evasion", re.compile(r"降重")),
    ("evasion", re.compile(r"降低重复率")),
    ("evasion", re.compile(r"重复率[^，。！？\n]{0,4}(降|太高|过高|偏高)")),
    ("evasion", re.compile(r"降\s*(ai|aigc)", re.IGNORECASE)),
    ("evasion", re.compile(r"(ai|aigc)\s*率", re.IGNORECASE)),
    ("evasion", re.compile(r"(去|消除|去掉|去除)\s*ai\s*(味|痕迹)", re.IGNORECASE)),
    ("evasion", re.compile(r"ai\s*(痕迹|味)", re.IGNORECASE)),
    ("evasion", re.compile(r"(规避|绕过|躲过|通过)\s*(查重|检测|turnitin)", re.IGNORECASE)),
    # --- whole-paper ghostwriting ----------------------------------------- #
    # Requires an explicit "whole/complete" marker or the word 代写/枪手. A bare
    # "帮我写毕业论文" is NOT here: it is ambiguous with the legitimate "帮我写
    # 毕业论文的方法部分", and a false refuse there would be worse than letting the
    # model route it (it will say `unclear`, and the user gets `clarify`).
    ("ghostwriting", re.compile(r"代写")),
    ("ghostwriting", re.compile(r"枪手")),
    ("ghostwriting", re.compile(r"完整[^，。！？\n]{0,4}(论文|文章|稿件|毕业设计)")),
    ("ghostwriting", re.compile(r"(整篇|全文)[^，。！？\n]{0,4}(论文|文章|代写|生成|写|毕业)")),
    ("ghostwriting", re.compile(r"(写|生成|来|弄)[^，。！？\n]{0,4}(整篇|全文)")),
    # --- copyright / ToS scraping ----------------------------------------- #
    # A verb of bulk extraction aimed at a protected source. "检索数据库" and
    # "采集实验数据" do not match: the verbs and the targets are both narrower than
    # the words that appear in legitimate retrieval requests.
    (
        "scraping",
        re.compile(
            r"(爬虫|抓取|爬取|采集|批量下载|批量抓取|批量采集)"
            r"[^，。！？\n]{0,10}"
            r"(知网|万方|维普|cnki|数据库|版权|全部论文|所有论文|全库)",
            re.IGNORECASE,
        ),
    ),
)

# Fixed, deterministic, no model call. Both state the boundary and then redirect to
# what the product *does* do, because a bare "no" teaches the user nothing about how
# to get help. The refuse message names the two things L0 caught (ghostwriting,
# evasion) and offers the legitimate neighbours (draft a section from your own
# claims, polish, citations) -- which is exactly the option-1 capability the routing
# layer is supposed to make reachable.
REFUSE_MESSAGE = (
    "我只能辅助你自己完成的学术写作，不能代写整篇论文，也不能做降重、"
    "降低重复率或规避查重/AI 检测这类操作——这些属于学术诚信问题，不在产品范围内。\n\n"
    "如果内容是你自己的，我可以帮你：梳理某一节的论证、在你提供论点或数据的前提下"
    "协助起草、润色语言表达、规划章节大纲，或整理参考文献格式。告诉我你具体想做哪一件。"
)

CLARIFY_MESSAGE = (
    "我没能确定你的需求。可以说得更具体些吗？我能在这些方向上帮你："
    "查找文献、规划论文或章节大纲、在你提供论点/数据的前提下协助起草某一节、"
    "润色你已有的段落，或整理参考文献格式（GB/T 7714、APA、IEEE 等）。"
)


def boundary_violation(query: str) -> BoundaryFamily | None:
    """The first boundary family `query` matches, or None.

    Pure and deterministic: the same string always yields the same answer, so the
    gate is reproducible and can be tested against the eval corpus without a
    model. Runs on the query alone, not on `ui_context.selected_text` -- the
    instruction lives in what the user typed, and the selection is their content,
    not a directive. Scanning the selection too is a possible extension, but it
    would also mean a boundary word quoted inside a passage the user is asking to
    polish could trip the gate, which is a false positive we do not want yet.
    """
    for family, pattern in _BOUNDARY_RULES:
        if pattern.search(query):
            return family
    return None


def guard_from(decisions: Decisions, request: RouteInput) -> Guard:
    """Decide proceed / clarify / refuse from the decision and the raw request.

    Order matters. The boundary check runs first and on the raw query, so it wins
    over the model: a leaked `intent` cannot smuggle an out-of-scope request past
    the gate. Only if the query is clean do we consult `decisions`, and there the
    two "don't proceed" signals collapse into `clarify` -- `needs_clarify` (the
    model judged the information insufficient) and `intent == "unclear"` (the model
    could not place it). Both mean "ask", and asking is the safe action: it costs
    one turn and it never fabricates or ghostwrites.

    `path_deep` is deliberately not consulted here. It selects Fast vs Deep Path,
    which is a *how*, not a *whether* -- and the Deep Path does not exist until
    Week 2. Folding it into the guard would conflate "do the deep work" with "don't
    do this at all".
    """
    if boundary_violation(request.query) is not None:
        return "refuse"
    if decisions.needs_clarify or decisions.intent == "unclear":
        return "clarify"
    return "proceed"
