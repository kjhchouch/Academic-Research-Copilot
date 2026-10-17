"""Chat model factory.

Takes plain arguments, never the gateway's `Settings` object. The dependency
runs apps/api -> packages/agent; importing api.config here would invert it and
make the orchestration core untestable outside the gateway. Same shape as
storage.make_checkpointer.

`api_key` is a plain `str | None` rather than a `SecretStr` for the same reason
config.py uses SecretStr: the secret should be masked everywhere it might be
repr'd or logged, and unwrapped at exactly one call site. That call site is the
gateway, not this module.
"""

from __future__ import annotations

from typing import Any


def make_chat_model(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: float = 60.0,
    max_retries: int = 2,
) -> Any:
    """Build an OpenAI-compatible chat model.

    DeepSeek speaks the OpenAI protocol, so one client covers it and repointing
    `base_url` switches to OpenAI / OpenRouter / a local Ollama without touching
    a single call site.

    Two DeepSeek specifics that are easy to get wrong:

    `max_tokens` must stay generous. Both DeepSeek models emit `reasoning_content`
    alongside `content`, and the reasoning tokens are billed against this budget.
    Set it to 256 and the model spends everything thinking and never emits the
    tool call -- which reads exactly like broken tool calling. It is not.
    Reasoning arrives in `additional_kwargs`, not `content`, so it never leaks
    into the streamed text.

    Diagnose truncation from `finish_reason`, never by reading the output. A
    response that looks complete and one that was cut off at the token cap are
    indistinguishable by eye.

    `max_retries=2` covers transport flakewness only. The OpenAI client also
    retries 429 and 5xx, which is what we want; anything else (bad key, no
    balance) fails fast and should stay failing fast.
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key or "",
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        streaming=True,
    )
