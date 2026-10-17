"""System prompt for the Fast Path agent.

Kept short on purpose, and for a reason that is not stylistic: this string is
the leading edge of the prompt-cache prefix. Everything after it varies per
request; everything before it must not, or the cache never hits. So nothing
that changes per user, per thread, or per day belongs here.

What is deliberately absent: the academic-integrity boundary (no ghostwriting,
no plagiarism-washing). That is enforced in the routing layer as a hard `refuse`
exit, not requested here as good behaviour. A prompt is a suggestion to a model;
a graph edge is a fact about the system. Boundaries that matter go in edges.
"""

from __future__ import annotations

FAST_PATH_SYSTEM_PROMPT = """\
你是一个科研写作辅助助手，帮助用户梳理论文思路、检查统计报告、润色段落。

你辅助，不代写：不生成用户没有提供的研究结果、数据、结论或参考文献。
需要事实性输入时，先向用户询问，不要编造。

回答用中文，简洁直接，不铺垫。
"""
