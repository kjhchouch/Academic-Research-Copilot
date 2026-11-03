r"""`cited_claims` 的切分契约。

这是 grounding 流水线的第一段纯函数：答案文本 -> 带引用号的句段。它必须和前端
`textWithChips` 的 `\[(\d+)\]` 窄正则保持同一口径——凡是前端不渲染成 chip 的标记
（`[K1]`、`[12, 15]`、`[注]`），这里也一概不认，两边才不会对同一份答案产生两套真相。
"""

from __future__ import annotations

from paper_agent.grounding.claims import CitedClaim, cited_claims


def test_chinese_sentence_with_trailing_marker() -> None:
    claims = cited_claims("对比学习提升了表征质量[3]。随后的工作扩展了这一结论。")
    assert claims == [CitedClaim(text="对比学习提升了表征质量[3]。", citation_id=3)]


def test_two_markers_in_one_sentence_yield_two_claims() -> None:
    claims = cited_claims("MoCo[1] 与 SimCLR[2] 都使用了动量编码器。")
    assert [(c.citation_id, c.text) for c in claims] == [
        (1, "MoCo[1] 与 SimCLR[2] 都使用了动量编码器。"),
        (2, "MoCo[1] 与 SimCLR[2] 都使用了动量编码器。"),
    ]


def test_line_is_the_unit_not_the_paragraph() -> None:
    """markdown 的换行是比句号更硬的边界：表格行和列表项不与上一句合并。"""
    answer = "结论是方法有效[1]。\n| 方法 | 准确率[2] |\n- 要点：来自[3]"
    claims = cited_claims(answer)
    assert [c.citation_id for c in claims] == [1, 2, 3]
    assert claims[1].text == "| 方法 | 准确率[2] |"
    assert claims[2].text == "- 要点：来自[3]"


def test_decimal_is_not_a_sentence_boundary() -> None:
    claims = cited_claims("准确率达到 95.5%[4]。明显优于基线。")
    assert [c.citation_id for c in claims] == [4]
    assert claims[0].text == "准确率达到 95.5%[4]。"


def test_western_period_splits_only_before_a_new_sentence() -> None:
    """西文句号只在后跟空白+大写/数字时才是边界；句号留在左段。"""
    claims = cited_claims("The method improved accuracy[5]. It also cut cost[6].")
    assert [c.citation_id for c in claims] == [5, 6]
    assert claims[0].text == "The method improved accuracy[5]."
    assert claims[1].text == "It also cut cost[6]."


def test_english_decimal_is_not_a_boundary() -> None:
    claims = cited_claims("Accuracy was 95.5%[4]. Better than the baseline[8].")
    assert [c.citation_id for c in claims] == [4, 8]
    assert claims[0].text == "Accuracy was 95.5%[4]."


def test_k_markers_and_compound_markers_do_not_match() -> None:
    """知识问答的 [K1] 与前端不渲染的 [12, 15] 不是文献引用，标注层不碰它们。"""
    assert cited_claims("如资料所述[K1]，另有综述[12, 15]可参考，还有[注]1 这种写法。") == []


def test_no_markers_returns_empty() -> None:
    assert cited_claims("你好，今天天气怎么样？") == []


def test_bare_marker_without_sentence_ender() -> None:
    claims = cited_claims("代表性工作见[3]")
    assert claims == [CitedClaim(text="代表性工作见[3]", citation_id=3)]


def test_empty_and_blank_input() -> None:
    assert cited_claims("") == []
    assert cited_claims("\n\n  \n") == []
