r"""Claim 抽取：把答案文本切成「带引用号的句段」。

这是 grounding 流水线的第一段，纯函数。单位是**句段**而不是整篇答案：Citation
Precision / Recall 定义在单个引用上（第 3 号引用有没有被它标的那句话支撑），
整篇一次判给不出这个粒度。

切分规则的两条口径，都是对着别处钉死的：

* **标记正则与前端同一条窄规则** `\[(\d+)\]`（见 apps/web 的 `textWithChips`）。
  `[K1]`（知识问答路径的来源引用，有自己的二阶段审核）、`[12, 15]`、`[注]` 一律不匹配。
  两边同口径，答案里的一段文字才不会在网关和前端各有一套「算不算引用」的真相。
* **行是硬边界**。markdown 的段落、表格行、列表项天然是独立单元；跨行合并句子
  只会把表格管道符搅进 claim 文本里，而对蕴含判断毫无帮助。

已知边界（刻意接受）：西文句号只在前瞻「空白 + 大写/数字」时视为边界，所以
`95.5` 不切、`e.g. The` 会切。后者在学术文本里存在，但把它判对需要分词表，
换来的只是句段切得更漂亮——claim 是否被 span 支撑与这个切法无关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 与 apps/web/src/lib/chatParts.ts 的 textWithChips 同口径：方括号内纯数字。
# [K1]（知识问答）、[12, 15]（区间）、[注]（文字注）都不匹配。
_MARKER = re.compile(r"\[(\d+)\]")

# 句段边界。中文/全角终止符直接切（零宽断言，终止符留在左段）；
# 西文句号用 lookbehind+lookahead 的零宽切点（句号留在左段），只在后跟
# 「空白 + 大写或数字」时切，避开小数和缩写点后的小写续句。
_CJK_END = re.compile(r"(?<=[。！？；!?])")
_LATIN_END = re.compile(r"(?<=\.)(?=\s+[A-Z0-9])")


@dataclass(frozen=True, slots=True)
class CitedClaim:
    """一条待核验的 claim：它出现的句段，和它指向的引用号。"""

    text: str
    citation_id: int


def cited_claims(answer: str) -> list[CitedClaim]:
    """答案文本里每个 `[n]` 对应一条 claim。

    一个句段含 n 个标记就产 n 条 claim（同一文本、不同 citation_id）：核验是
    「这句话是否被第 n 篇文献支撑」，两个引用就是两个独立问题。无标记的答案
    （寒暄、润色、纯格式化）返回空列表——没有 claim 就没有调用，grounding 对
    无引用轮次是零开销的。
    """
    claims: list[CitedClaim] = []
    for line in answer.splitlines():
        for segment in _segments(line):
            for match in _MARKER.finditer(segment):
                claims.append(CitedClaim(text=segment.strip(), citation_id=int(match.group(1))))
    return claims


def _segments(line: str) -> list[str]:
    """一行 -> 句段列表，终止符留在左段，空段丢弃。"""
    parts: list[str] = []
    for chunk in _CJK_END.split(line):
        parts.extend(_LATIN_END.split(chunk))
    return [part for part in parts if part.strip()]
