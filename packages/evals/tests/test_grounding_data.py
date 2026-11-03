"""grounding.jsonl 的 schema 门禁。

与 test_routing_data.py 同一角色：数据集是指标的上游，数据坏了指标就是幻觉。
这里比 routing 多一条**provenance 纪律**——note 必填。routing 只要求 boundary 行
写 note，因为 routing 语料的每一行都自解释；grounding 语料的 span 有两种来源
（真实 fixture 摘要 / 合成），不在行里写清楚，三个月后没有人能分清哪条数字是
测真实分布、哪条是测构造成分。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from paper_agent.grounding import MODEL_VERDICTS, VERDICTS

DATA = Path(__file__).resolve().parents[1] / "data" / "grounding.jsonl"

REQUIRED_KEYS = {"id", "answer_id", "claim", "span", "expect", "note"}
# 评测只考模型层的三值；unbacked 是 L0 查表产物，没有 (claim, span) 判例可言，
# 所以 expect 的合法集合是 MODEL_VERDICTS 而不是 VERDICTS 全集。
VALID_EXPECTS = set(MODEL_VERDICTS)


def load_rows(path: Path = DATA) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rows.append((lineno, json.loads(line)))
    return rows


def test_dataset_exists_and_is_nonempty() -> None:
    assert len(load_rows()) >= 20, "语料 <20 说明又被清空了；要长就刻意长，别清空重编"


def test_every_row_has_the_full_schema() -> None:
    for lineno, row in load_rows():
        missing = REQUIRED_KEYS - set(row)
        extra = set(row) - REQUIRED_KEYS
        assert not missing, f"line {lineno}: missing {sorted(missing)}"
        assert not extra, f"line {lineno}: unknown keys {sorted(extra)} — schema 漂移"


def test_expect_is_within_the_model_verdict_vocabulary() -> None:
    for lineno, row in load_rows():
        assert row["expect"] in VALID_EXPECTS, (
            f"line {lineno}: expect={row['expect']!r} 不在模型词表 {VALID_EXPECTS}；"
            f"unbacked 是 L0 专属，不进 (claim, span) 评测"
        )


def test_notes_are_nonempty_and_carry_provenance() -> None:
    for lineno, row in load_rows():
        note = row["note"].strip()
        assert note, f"line {lineno}: note 必填（provenance / expect 理由）"
        assert any(token in note for token in ("真实摘要", "合成 span", "fixture")), (
            f"line {lineno}: note 必须声明 span 来源（真实 fixture / 合成）"
        )


def test_ids_are_unique_and_grouped() -> None:
    rows = [row for _, row in load_rows()]
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "id 重复：行不可寻址，评测报告会指不出哪条错了"
    sizes = Counter(row["answer_id"] for row in rows)
    assert min(sizes.values()) >= 2, "每个 answer 组至少 2 行，组级 P/R 才有定义"
    # 三个类都得出现，且每个组内至少有两类——否则组级 precision/recall 退化成常数。
    assert set(row["expect"] for row in rows) == VALID_EXPECTS
    for answer_id in sizes:
        expects = set(row["expect"] for row in rows if row["answer_id"] == answer_id)
        assert len(expects) >= 2, f"answer 组 {answer_id} 只有一类 expect，P/R 无意义"


def test_claims_and_spans_are_substantive() -> None:
    for lineno, row in load_rows():
        assert len(row["claim"]) >= 10, f"line {lineno}: claim 太短，蕴含无从谈起"
        assert len(row["span"]) >= 40, f"line {lineno}: span 太短，判例退化为关键词匹配"


def test_verdict_vocabulary_matches_the_agent_package() -> None:
    """evals 不该 import 被它评分的 backend，但**词表**必须两边一致——
    routing 的 INTENTS 交叉测试是同一纪律：清单漂移时，准确率数字静默失真。"""
    assert set(VERDICTS) == {"supported", "uncertain", "contradicted", "unbacked"}
    assert set(MODEL_VERDICTS) == {"supported", "uncertain", "contradicted"}
