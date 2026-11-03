"""Grounding eval harness：给蕴含后端打分，并算出 README 承诺的 Citation Precision / Recall。

与 routing.py 同构的三件套，理由也在那边论证过：

**`score` 是 `(rows, predictions) -> Report` 的纯函数。** 指标算术离线可测——确定性、
无 key、无网络、不烧余额；算术 bug 被单测当场抓住，而不是等数字进了简历才暴露。

**harness 与具体后端解耦，但出处强制入报告。** `run_eval` 接受任何有
`judge(pairs) -> list[Judgement]` 的对象；每份 `Report` 带 `by_backend`。生产档是
Jev（README 技术栈：意图路由 / grounding 蕴含校验 / 注入检测），今天能跑的只有
`small_llm` 降级档——`by_backend` 让降级档的数字**自带出处**，没法被悄悄当成
生产档的。

**errored 组整组记 miss，不剔除。** 一个组判不了（后端抛异常）意味着该组引用的
标注整体缺失——在产品里那正是 fail-open 的 uncertain 洪流，评测就该反映它。

指标分两层，边界要读清楚：

1. **后端质量**（这个 harness 直接测的）：三值 accuracy / per-class P/R / macro-F1，
   加**危险方向泄漏率** = `predicted=supported ∧ expect≠supported`。它直接上界了
   产品 Citation Precision 的测量误差——把假的判成真的，比把真的判成假的糟：
   前者让幻觉引用带着绿色 chip 出门，后者只是多一个警告。**这与 routing 把 errored
   边界行记泄漏的方向相反、且是对称的正确**：routing 的 errored 是「没拒成」，
   这里的 errored 是「没断言」——uncertain 是安全输出，None 不是断言。
2. **Citation Precision / Recall**（README 评测表的两行）：按 `answer_id` 分组，
   对 supported 类取 P/R，组间宏平均。**这是从预测算的**——它演示的是产品运行时
   看板上那个数字的计算口径。地真的（expect）在数据构造时就定义了答案的引用
   全部「该支持的都支持、不该支持的不支持」，所以 from-expect 版本恒为 1，没有
   信息量；真正回答「产品数字可信吗」的是第 1 层的后端质量指标 + 泄漏率。
   端到端的产品级数字需要对真实用户答案跑 live 评测，那需要人工标注，另列。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from paper_agent.grounding import ClaimSpan, Judgement

DATA = Path(__file__).resolve().parents[2] / "data" / "grounding.jsonl"

MODEL_VERDICTS: tuple[str, ...] = ("supported", "uncertain", "contradicted")
SUPPORTED = "supported"


class EntailmentBackend(Protocol):
    """评测需要的后端切片。`name` 进 `by_backend`，是出处强制的一部分。"""

    name: str

    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]: ...


@dataclass(frozen=True, slots=True)
class Row:
    """一行判例。`expect` 是地真；`note` 在数据门禁里已强制 provenance。"""

    id: str
    answer_id: str
    claim: str
    span: str
    expect: str
    note: str

    def as_pair(self, index: int) -> ClaimSpan:
        """`citation_id` 在这里没有产品语义，只用来把判定对回行——用行号。"""
        return ClaimSpan(claim=self.claim, citation_id=index, span=self.span)


def load_rows(path: Path | None = None) -> list[Row]:
    src = path or DATA
    rows: list[Row] = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        rows.append(
            Row(
                id=raw["id"],
                answer_id=raw["answer_id"],
                claim=raw["claim"],
                span=raw["span"],
                expect=raw["expect"],
                note=raw.get("note", ""),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class ClassStats:
    """supported / uncertain / contradicted 一类的混淆矩阵格子。

    分母为空取 0.0（sklearn `zero_division=0` 约定）；`support == 0` 的类不进
    macro 平均——零支持度的类没有定义 recall，把 0.0 塞进均值会让数字随词表
    大小漂移而不是随质量变化。
    """

    label: str
    support: int
    predicted: int
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True, slots=True)
class Report:
    """一次评测的全部结果。可序列化（`as_dict`），可归档。"""

    n_rows: int
    n_errors: int
    accuracy: float
    macro_f1: float
    per_class: dict[str, ClassStats]
    # predicted=supported 但 expect 不是——危险方向泄漏。
    leakage: LeakageScore
    # README 的 Citation Precision / Recall：按 answer 组宏平均，从预测算。
    citation_precision: float
    citation_recall: float
    n_answer_groups: int
    by_backend: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_errors": self.n_errors,
            "accuracy": round(self.accuracy, 4),
            "macro_f1": round(self.macro_f1, 4),
            "leakage": {
                "n": self.leakage.n,
                "leaked": self.leakage.leaked,
                "rate": None if self.leakage.rate is None else round(self.leakage.rate, 4),
            },
            "citation_precision": round(self.citation_precision, 4),
            "citation_recall": round(self.citation_recall, 4),
            "n_answer_groups": self.n_answer_groups,
            "by_backend": dict(self.by_backend),
            "per_class": {
                label: {
                    "support": s.support,
                    "predicted": s.predicted,
                    "precision": round(s.precision, 4),
                    "recall": round(s.recall, 4),
                    "f1": round(s.f1, 4),
                }
                for label, s in self.per_class.items()
            },
        }


@dataclass(frozen=True, slots=True)
class LeakageScore:
    n: int
    leaked: int

    @property
    def rate(self) -> float | None:
        return self.leaked / self.n if self.n else None


def score(
    rows: Sequence[Row],
    predictions: Sequence[Judgement | None],
    *,
    backend_name: str = "unknown",
) -> Report:
    """从地真与后端判定算出全部指标。predictions[i] 对应 rows[i]，None = 该行 errored。

    `backend_name` 只进 `by_backend`（出处），不参与任何算术——score 因此保持
    纯函数；run_eval 从后端对象读名传入。
    """
    if len(rows) != len(predictions):
        raise ValueError(
            f"rows and predictions must be parallel: {len(rows)} vs {len(predictions)}"
        )
    pairs = list(zip(rows, predictions, strict=True))
    n_errors = sum(1 for _, p in pairs if p is None)

    def verdict_of(index: int) -> str | None:
        prediction = predictions[index]
        return prediction.verdict if prediction is not None else None

    # --- accuracy ------------------------------------------------------------ #
    correct = sum(
        1 for i, (row, p) in enumerate(pairs) if p is not None and p.verdict == row.expect
    )
    accuracy = correct / len(rows) if rows else 0.0

    # --- per-class confusion -------------------------------------------------- #
    per_class: dict[str, ClassStats] = {}
    for label in MODEL_VERDICTS:
        support = sum(1 for row, _ in pairs if row.expect == label)
        predicted = sum(1 for i, (_, p) in enumerate(pairs) if p is not None and p.verdict == label)
        tp = sum(
            1
            for i, (row, p) in enumerate(pairs)
            if p is not None and row.expect == label and p.verdict == label
        )
        fp = predicted - tp
        fn = support - tp
        per_class[label] = ClassStats(
            label=label, support=support, predicted=predicted, tp=tp, fp=fp, fn=fn
        )

    graded = [per_class[label] for label in MODEL_VERDICTS if per_class[label].support > 0]
    macro_f1 = sum(s.f1 for s in graded) / len(graded) if graded else 0.0

    # --- dangerous-direction leakage ----------------------------------------- #
    # 只统计**显式的错误 supported 断言**。errored（None）不是断言——生产里它
    # 变成 uncertain（fail-open），是安全输出，与 routing「没拒成即泄漏」的方向
    # 相反且各自正确，见模块 docstring。
    leakage_rows = [(i, row) for i, (row, _) in enumerate(pairs) if row.expect != SUPPORTED]
    leaked = sum(1 for i, _ in leakage_rows if verdict_of(i) == SUPPORTED)
    leakage = LeakageScore(n=len(leakage_rows), leaked=leaked)

    # --- Citation Precision / Recall（README 口径）---------------------------- #
    # 按 answer 组取 supported 类的 P/R，组间宏平均；组内分母为空计 0.0（该组
    # 的引用标注在那一侧整体失败，不该从均值里消失）。
    group_ids = []
    seen: set[str] = set()
    for row in rows:
        if row.answer_id not in seen:
            seen.add(row.answer_id)
            group_ids.append(row.answer_id)
    precisions: list[float] = []
    recalls: list[float] = []
    for answer_id in group_ids:
        indices = [i for i, (row, _) in enumerate(pairs) if row.answer_id == answer_id]
        predicted_set = {i for i in indices if verdict_of(i) == SUPPORTED}
        truth_set = {i for i in indices if rows[i].expect == SUPPORTED}
        hits = predicted_set & truth_set
        precisions.append(len(hits) / len(predicted_set) if predicted_set else 0.0)
        recalls.append(len(hits) / len(truth_set) if truth_set else 0.0)
    citation_precision = sum(precisions) / len(precisions) if precisions else 0.0
    citation_recall = sum(recalls) / len(recalls) if recalls else 0.0

    # --- 出处 ---------------------------------------------------------------- #
    # None（errored）不进 by_backend：没判出来的行没有后端产出可言，但
    # n_errors 已经把它们的代价记在明处。
    by_backend = Counter({backend_name: sum(1 for p in predictions if p is not None)})

    return Report(
        n_rows=len(rows),
        n_errors=n_errors,
        accuracy=accuracy,
        macro_f1=macro_f1,
        per_class=per_class,
        leakage=leakage,
        citation_precision=citation_precision,
        citation_recall=citation_recall,
        n_answer_groups=len(group_ids),
        by_backend=dict(by_backend),
    )


# run_eval 读后端名并作为参数传给 score——score 保持纯函数，出处是评测运行的
# 属性而不是 Judgement 的职责。


async def run_eval(backend: EntailmentBackend, rows: Sequence[Row]) -> Report:
    """按 answer 组驱动后端并打分。

    按**组**而不是按行驱动，是在模拟生产调用形状：产品对一条答案的全部引用做
    一次批量判定，`SmallLLMEntailment` 在其内部按批上限串行分批。整组异常 ->
    该组每行记 None（miss），其余组照常——一行/一组失配不能连累整份数据集。
    """
    backend_name = getattr(backend, "name", "unknown") or "unknown"
    predictions: list[Judgement | None] = [None] * len(rows)
    group_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.answer_id not in seen:
            seen.add(row.answer_id)
            group_ids.append(row.answer_id)

    for answer_id in group_ids:
        indices = [i for i, row in enumerate(rows) if row.answer_id == answer_id]
        pairs = [rows[i].as_pair(local) for local, i in enumerate(indices)]
        try:
            judgements = await backend.judge(pairs)
        except Exception:  # noqa: BLE001 - 一组失败不连累整份评测
            continue
        if len(judgements) != len(indices):
            continue
        for local, judgement in enumerate(judgements):
            predictions[indices[local]] = judgement

    return score(rows, predictions, backend_name=backend_name)
