"""Prompt templates.

Keep every prompt in this package rather than inline in nodes, so that prompt
changes are diffable and can be regression-tested by packages/evals.
"""

from paper_agent.prompts.system import FAST_PATH_SYSTEM_PROMPT

__all__ = ["FAST_PATH_SYSTEM_PROMPT"]
