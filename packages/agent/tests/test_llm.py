"""`make_chat_model` thinking-mode wiring -- the multi-turn 400 fix (task #36).

These pin the `extra_body` the factory sends, without a live call. The error they
prevent is DeepSeek's "The `reasoning_content` in the thinking mode must be
passed back to the API", which fires on the *second* turn of a thread whose first
turn made a tool call: thinking mode emits `reasoning_content`, the checkpointer
round-trip drops it, and DeepSeek then rejects the history. Disabling thinking on
the fast path removes the field entirely, so there is nothing to pass back. See
the factory docstring for the full mechanism.
"""

from __future__ import annotations

from typing import Any

from paper_agent.llm import make_chat_model


def _model(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "base_url": "https://api.deepseek.com",
        "api_key": "test-key-not-a-secret",
        "model": "deepseek-flash",
    }
    kwargs.update(overrides)
    return make_chat_model(**kwargs)


def test_thinking_is_disabled_by_default() -> None:
    """The fast path must not enter thinking mode -- that is the whole fix.

    If this default ever flips to enabled without a matching checkpoint
    round-trip for `reasoning_content`, every multi-turn thread starts 400ing
    again on its second message.
    """
    assert _model().extra_body == {"thinking": {"type": "disabled"}}


def test_thinking_true_enables_for_a_future_deep_path() -> None:
    assert _model(thinking=True).extra_body == {"thinking": {"type": "enabled"}}


def test_thinking_none_omits_the_field_entirely() -> None:
    """A non-DeepSeek `base_url` must not receive the unknown `thinking` param.

    OpenAI's API 400s on unrecognized body parameters, so the escape hatch from
    the DeepSeek-specific default is `None`, not `False`.
    """
    extra_body = _model(thinking=None).extra_body
    assert not extra_body  # None or empty: either way nothing is sent
