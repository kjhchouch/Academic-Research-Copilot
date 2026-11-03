"""`score()` 纯函数的指标算术 + `run_eval` 的组驱动契约。

和 test_routing_eval.py 同一条存在理由：这里的每个期望值都是**手算**的 toy 集。
算术错了，最坏的去向不是测试红，而是一个看着体面的数字进了 README——手算
toy 集就是防它的。 erste 版手算自己错过的坑（把 uncertain 的 predicted 数错）
正是这套测试要永久钉住的东西。
"""

from __future__ import annotations

from typing import Any

import pytest
from evals.grounding import Row, run_eval, score
from paper_agent.grounding import Judgement


def _row(rid: str, answer_id: str, expect: str) -> Row:
    return Row(
        id=rid,
        answer_id=answer_id,
        claim=f"主张 {rid}",
        span=f"span {rid} " + "x" * 50,
        expect=expect,
        note="toy",
    )


def _pred(verdict: str) -> Judgement:
    return Judgement(citation_id=0, verdict=verdict, reason=None)  # type: ignore[arg-type]


TOY_ROWS = [
    _row("a1", "a", "supported"),
    _row("a2", "a", "supported"),
    _row("a3", "a", "uncertain"),
    _row("a4", "a", "contradicted"),
    _row("b1", "b", "supported"),
    _row("b2", "b", "contradicted"),
]

# 手算（见 test_toy_set 各断言）：
#   r0 sup→sup ✓  r1 sup→unc ✗  r2 unc→unc ✓  r3 con→con ✓  r4 sup→sup ✓  r5 con→sup ✗(泄漏)
TOY_PREDICTIONS = [
    _pred("supported"),
    _pred("uncertain"),
    _pred("uncertain"),
    _pred("contradicted"),
    _pred("supported"),
    _pred("supported"),
]


def test_toy_set_hand_computed_core_rates() -> None:
    report = score(TOY_ROWS, TOY_PREDICTIONS, backend_name="toy")
    assert report.n_rows == 6
    assert report.n_errors == 0
    assert report.accuracy == pytest.approx(4 / 6)
    assert report.by_backend == {"toy": 6}


def test_toy_set_hand_computed_per_class_and_macro() -> None:
    report = score(TOY_ROWS, TOY_PREDICTIONS, backend_name="toy")
    supported = report.per_class["supported"]
    assert (supported.support, supported.predicted, supported.tp, supported.fp, supported.fn) == (
        3,
        3,
        2,
        1,
        1,
    )
    assert supported.precision == pytest.approx(2 / 3)
    assert supported.recall == pytest.approx(2 / 3)
    uncertain = report.per_class["uncertain"]
    assert uncertain.precision == pytest.approx(0.5)
    assert uncertain.recall == pytest.approx(1.0)
    contradicted = report.per_class["contradicted"]
    assert contradicted.precision == pytest.approx(1.0)
    assert contradicted.recall == pytest.approx(0.5)
    # 三类 f1 各 2/3 → macro 同。
    assert report.macro_f1 == pytest.approx(2 / 3)


def test_toy_set_hand_computed_leakage_and_citation_pr() -> None:
    report = score(TOY_ROWS, TOY_PREDICTIONS, backend_name="toy")
    # 泄漏只数 expect != supported 的行（r2/r3/r5），其中被判成 supported 的只有 r5。
    assert report.leakage.n == 3
    assert report.leakage.leaked == 1
    assert report.leakage.rate == pytest.approx(1 / 3)
    # 组 a：预测 supported {a1}，真值 {a1,a2} → P=1.0, R=0.5
    # 组 b：预测 {b1,b2}，真值 {b1} → P=0.5, R=1.0；宏平均各 0.75。
    assert report.n_answer_groups == 2
    assert report.citation_precision == pytest.approx(0.75)
    assert report.citation_recall == pytest.approx(0.75)


def test_errored_rows_are_misses_but_never_leaks() -> None:
    """None 不是断言：accuracy 里记 miss，泄漏里**不**记——与 routing 的方向刻意
    相反（routing 的 errored 是『没拒成』，这里的 errored 是『没断言』）。"""
    predictions: list[Any] = [
        _pred("supported"),
        _pred("uncertain"),
        None,
        _pred("contradicted"),
        _pred("supported"),
        None,
    ]
    report = score(TOY_ROWS, predictions, backend_name="toy")
    assert report.n_errors == 2
    assert report.accuracy == pytest.approx(3 / 6)
    assert report.leakage.n == 3
    assert report.leakage.leaked == 0
    assert report.by_backend == {"toy": 4}  # errored 行没有后端产出，不进出处计数
    # 组 b 只剩 b1 被判 supported 且真值 supported → P=R=1.0。
    assert report.citation_precision == pytest.approx((1.0 + 1.0) / 2)
    assert report.citation_recall == pytest.approx((0.5 + 1.0) / 2)


def test_zero_support_class_is_excluded_from_macro() -> None:
    rows = [
        _row("a1", "a", "supported"),
        _row("a2", "a", "supported"),
        _row("b1", "b", "supported"),
    ]
    predictions = [_pred("supported"), _pred("supported"), _pred("supported")]
    report = score(rows, predictions, backend_name="toy")
    assert report.per_class["contradicted"].support == 0
    assert report.per_class["contradicted"].recall == 0.0  # zero_division=0 约定
    assert report.macro_f1 == pytest.approx(1.0)  # 只有 supported 进均值


def test_group_with_no_predicted_supported_gets_zero_precision() -> None:
    """整组被判 uncertain → 该组没有 supported 断言，precision 计 0 而不是被剔除：
    标注层在那一组整体失职，均值必须反映它。"""
    rows = [
        _row("a1", "a", "supported"),
        _row("b1", "b", "supported"),
    ]
    predictions = [_pred("supported"), _pred("uncertain")]
    report = score(rows, predictions, backend_name="toy")
    assert report.citation_precision == pytest.approx((1.0 + 0.0) / 2)
    assert report.citation_recall == pytest.approx((1.0 + 0.0) / 2)


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="parallel"):
        score(TOY_ROWS, TOY_PREDICTIONS[:3])


class _ScriptedBackend:
    """按**调用顺序**弹出脚本的假后端：run_eval 以首次出现的组序驱动（a、b），
    所以脚本顺序即预期调用顺序。"""

    name = "scripted"

    def __init__(
        self, script: list[tuple[str, list[str]]], *, explode: set[str] | None = None
    ) -> None:
        self._script = list(script)
        self._explode = explode or set()
        self.calls: list[tuple[str, list[Any]]] = []

    async def judge(self, pairs: list[Any]) -> list[Judgement]:
        group, verdicts = self._script.pop(0)
        self.calls.append((group, pairs))
        if group in self._explode:
            raise RuntimeError("boom")
        return [
            Judgement(citation_id=p.citation_id, verdict=verdict, reason=None)  # type: ignore[arg-type]
            for p, verdict in zip(pairs, verdicts, strict=True)
        ]


async def test_run_eval_drives_by_answer_group_and_isolates_failures() -> None:
    backend = _ScriptedBackend(
        [
            ("a", ["supported", "uncertain", "uncertain", "contradicted"]),
            ("b", ["supported", "supported"]),
        ]
    )
    report = await run_eval(backend, TOY_ROWS)
    assert [group for group, _ in backend.calls] == ["a", "b"]
    assert [len(chunk) for _, chunk in backend.calls] == [4, 2]
    assert report.n_errors == 0
    assert report.by_backend == {"scripted": 6}
    assert report.accuracy == pytest.approx(4 / 6)


async def test_run_eval_records_an_exploding_group_as_all_misses() -> None:
    backend = _ScriptedBackend(
        [("a", ["supported"] * 4), ("b", ["supported", "supported"])], explode={"a"}
    )
    report = await run_eval(backend, TOY_ROWS)
    assert report.n_errors == 4
    assert report.by_backend == {"scripted": 2}  # b 组照常，a 组全 miss
    # b 组：b1 sup→sup ✓，b2 con→sup ✗ —— 全卷只有 1 条对。
    assert report.accuracy == pytest.approx(1 / 6)


async def test_run_eval_treats_a_shape_mismatched_group_as_misses() -> None:
    class _WrongCount:
        name = "wrong"

        async def judge(self, pairs: list[Any]) -> list[Judgement]:
            return [Judgement(citation_id=0, verdict="supported", reason=None)]  # type: ignore[arg-type]

    report = await run_eval(_WrongCount(), TOY_ROWS)
    assert report.n_errors == 6
    assert report.by_backend == {"wrong": 0}
