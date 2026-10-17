"""In-process tools.

Everything here is deterministic, offline and side-effect free. That is a
requirement for Week 1, not a preference: a tool the tests can run without a
network or an API key is a tool that can be in the CI suite, and the ReAct loop
is only actually verified if the tool leg of it really executes.

Retrieval tools arrive in D5 and are not like this -- they hit the network and
return untrusted text. They go behind MCP in a separate process, and their
output is treated as data, never as instructions.
"""

from __future__ import annotations

import re

from langchain_core.tools import tool

# CJK Unified Ideographs plus the two extension blocks that actually occur in
# academic Chinese. Counting these separately from latin words matters because
# journal length limits are expressed in one or the other, and "字数" for a
# Chinese manuscript means characters, not whitespace-delimited tokens.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)*")


@tool
def count_words(text: str) -> dict[str, int]:
    """统计一段文本的字数：中文字符数、英文单词数、总字符数。

    当用户询问某段文字有多少字、是否超出期刊字数限制、或需要压缩到指定长度时调用。
    参数 text 必须是需要统计的原文，逐字传入，不要改写或截断。
    """
    cjk = len(_CJK.findall(text))
    latin = len(_LATIN_WORD.findall(text))
    return {
        "cjk_characters": cjk,
        "latin_words": latin,
        # What a word processor reports: no whitespace, everything else counted.
        "total_characters": len(re.sub(r"\s", "", text)),
        # The number a Chinese journal's 字数 limit is usually applied to.
        "word_count": cjk + latin,
    }


TOOLS = [count_words]
