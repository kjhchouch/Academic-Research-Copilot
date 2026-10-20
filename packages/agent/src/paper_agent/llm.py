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
    thinking: bool | None = False,
) -> Any:
    """Build an OpenAI-compatible chat model.

    DeepSeek speaks the OpenAI protocol, so one client covers it and repointing
    `base_url` switches to OpenAI / OpenRouter / a local Ollama without touching
    a single call site.

    `thinking` toggles DeepSeek thinking mode. It is a top-level body field the
    OpenAI SDK does not model, so it travels in `extra_body`:

        False (default) -> {"thinking": {"type": "disabled"}}
        True            -> {"thinking": {"type": "enabled"}}
        None            -> field omitted (for a non-DeepSeek `base_url`; OpenAI
                           rejects unknown body params, so we must not send it).

    The default is disabled, and that is load-bearing rather than a preference.
    In thinking mode DeepSeek emits `reasoning_content` and *requires* it be
    echoed back on every later turn that involves tool calls. Within one run the
    in-process message keeps it in `additional_kwargs`, but a message restored
    from the checkpointer does not -- langchain-openai drops the non-standard
    field when it re-serializes -- so the *second* turn on a thread 400s with
    "The `reasoning_content` in the thinking mode must be passed back to the
    API". The fast path neither needs that reasoning nor can reliably round-trip
    it, so it turns thinking off. A future deep path that wants thinking must
    pass `thinking=True` *and* persist `reasoning_content` across the checkpoint,
    or it inherits this exact bug.

    `max_tokens` must stay generous when thinking is enabled: the reasoning
    tokens are billed against this budget, so a cap of 256 lets the model spend
    everything thinking and never emit the tool call -- which reads exactly like
    broken tool calling. It is not. Reasoning arrives in `additional_kwargs`, not
    `content`, so it never leaks into the streamed text.

    Diagnose truncation from `finish_reason`, never by reading the output. A
    response that looks complete and one that was cut off at the token cap are
    indistinguishable by eye.

    `max_retries=2` covers transport flakewness only. The OpenAI client also
    retries 429 and 5xx, which is what we want; anything else (bad key, no
    balance) fails fast and should stay failing fast.
    """
    from langchain_openai import ChatOpenAI

    extra_body: dict[str, Any] | None = None
    if thinking is not None:
        extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}

    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key or "",
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        streaming=True,
        extra_body=extra_body,
    )
