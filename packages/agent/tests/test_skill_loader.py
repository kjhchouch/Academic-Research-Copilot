"""`SkillCatalog` 的映射与 pack 组装契约。

映射键 ⊆ INTENTS 的交叉测试沿用 routing 的 INTENTS/eval 双清单纪律：这张表决定哪些
意图背上 pack 的成本，词汇表一变而表不变（或反之），可达性就静默漂移。体积门禁
（≤40K chars）防的是 re-vendor 把 always_load 撑大后 latency 在用户侧悄悄变慢——
那要在 CI 上红，不在生产上发生。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from paper_agent.routing.decision_model import INTENTS
from paper_agent.skill_loader import INTENT_SKILLS, SkillCatalog

REPO_ROOT = Path(__file__).resolve().parents[3]
VENDOR_ROOT = REPO_ROOT / "skills" / "third_party"


@pytest.fixture(scope="module")
def catalog() -> SkillCatalog:
    if not VENDOR_ROOT.is_dir():
        pytest.skip(f"vendored skills not present at {VENDOR_ROOT}")
    return SkillCatalog(VENDOR_ROOT)


def test_intent_mapping_keys_are_within_the_vocabulary() -> None:
    """映射表只能引用路由词汇表里真实存在的意图；词汇表收缩时这里必须先改。"""
    assert set(INTENT_SKILLS) <= set(INTENTS)
    assert INTENT_SKILLS, "映射表为空 = 没有任何 skill 可达，D10 白做"


def test_intents_without_skills_are_explicit() -> None:
    """literature_search 走 MCP 工具、unclear 是 guard 出口——它们刻意没有 skill。"""
    catalog = SkillCatalog(VENDOR_ROOT)
    assert catalog.skill_for_intent("literature_search") is None
    assert catalog.skill_for_intent("unclear") is None
    assert catalog.skill_for_intent("knowledge_qa") is None
    assert catalog.skill_for_intent(None) is None


def test_mapping_targets_exist_on_disk() -> None:
    for skill_name in set(INTENT_SKILLS.values()):
        assert (VENDOR_ROOT / skill_name / "SKILL.md").is_file(), (
            f"{skill_name} 在映射表里但磁盘上没有——要么补 vendor，要么删映射"
        )


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("polish", "nature-polishing"),
        ("draft_section", "nature-writing"),
        ("outline", "nature-writing"),
        ("citation_format", "nature-ref-verifier"),
    ],
)
def test_skill_for_intent(catalog: SkillCatalog, intent: str, expected: str) -> None:
    spec = catalog.skill_for_intent(intent)
    assert spec is not None and spec.name == expected


def test_disabled_catalog_maps_everything_to_none() -> None:
    catalog = SkillCatalog(VENDOR_ROOT, enabled=False)
    assert catalog.skill_for_intent("polish") is None
    assert catalog.pack_for_intent("polish") is None


def test_missing_root_degrades_to_none(tmp_path: Path) -> None:
    catalog = SkillCatalog(tmp_path / "nowhere")
    assert catalog.skill_for_intent("polish") is None
    assert catalog.pack_for_intent("polish") is None


def test_pack_is_deterministic(catalog: SkillCatalog) -> None:
    """pack 只随意图变化：同一意图两次构建必须逐字节相等——这是它插在 system prompt
    之后还能命中 prompt cache 的前提。"""
    assert catalog.pack_for_intent("polish") == catalog.pack_for_intent("polish")


def test_writing_pack_carries_the_narrowing_contract(catalog: SkillCatalog) -> None:
    pack = catalog.pack_for_intent("draft_section")
    assert pack is not None
    # 收窄是我们自己的文本，必须声明对上游内容有优先级。
    assert "优先于下文 skill 内容" in pack
    assert "章节级" in pack
    assert "先向用户要" in pack or "先向用户询问" in pack
    # 上游正文在：SKILL.md 的 frontmatter 与 always_load 材料都可辨认。
    assert "nature-writing" in pack
    assert "always_load" in pack or "reader-workflow" in pack


def test_pack_embeds_skill_md_and_always_load_bodies(catalog: SkillCatalog) -> None:
    pack = catalog.pack_for_intent("polish")
    assert pack is not None
    assert "=== nature-polishing/SKILL.md ===" in pack
    # SKILL.md（6K）+ always_load（13.4K）的量级：光有 SKILL.md 到不了这个数。
    assert len(pack) > 15_000
    # always_load 文件的正文确实进了 pack——每个文件带路径头，数一数头的数量。
    file_headers = [
        line for line in pack.splitlines() if line.startswith("=== ") and line.endswith(" ===")
    ]
    assert len(file_headers) >= 3  # SKILL.md + 至少两个 always_load 材料
    # 兄弟引用（../nature-shared/...）按 manifest 原样加载成功。
    assert "=== nature-polishing/../nature-shared/" in pack


def test_pack_size_gate_on_the_real_tree(catalog: SkillCatalog) -> None:
    """映射内每个 skill 的 pack ≤ 40K chars：re-vendor 膨胀在 CI 红，不在生产变慢。"""
    from paper_agent.skill_loader import MAX_PACK_CHARS

    for skill_name in set(INTENT_SKILLS.values()):
        pack = catalog.pack_for_skill_name(skill_name)
        assert pack is not None, f"{skill_name} 的 pack 构建失败"
        assert len(pack) <= MAX_PACK_CHARS, (
            f"{skill_name} pack {len(pack)} chars 超过门禁 {MAX_PACK_CHARS}——"
            "re-vendor 撑大了 always_load；先评估成本再调门禁"
        )


def test_pack_for_intent_never_raises_on_broken_tree(tmp_path: Path) -> None:
    """always_load 指向缺失文件时 pack 降级（占位警告行），不炸——磁盘完整性由
    vendored 门禁测试看守，运行时只求可见。"""
    skill = tmp_path / "nature-x"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: nature-x\n---\n正文", encoding="utf-8")
    (skill / "manifest.yaml").write_text("always_load:\n  - missing.md\n", encoding="utf-8")
    catalog = SkillCatalog(tmp_path)
    # nature-x 不在映射表里，借 polish 的意图测 pack_for_skill_name 的降级路径。
    pack = catalog.pack_for_skill_name("nature-x")
    assert pack is not None
    assert "missing.md" in pack  # 占位行保留了路径，缺失可辨
