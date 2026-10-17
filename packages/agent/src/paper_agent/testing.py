"""Test doubles for the agent core.

Lives in the package rather than in a tests/ directory because both
packages/agent/tests and apps/api/tests need it, and a cross-package import from
one tests/ tree into another does not survive being installed as a wheel.

Why this exists at all: LangChain's own fakes cannot drive a tool-calling graph.
`GenericFakeChatModel.bind_tools` raises NotImplementedError (langchain-core
1.6.10), and every other built-in fake inherits that. So the ReAct loop could
only be tested against a live API -- which means no CI, no determinism, and a
cost per test run. This is the smallest thing that fixes that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.messages.ai import AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed cycle of AIMessages, streaming text responses in chunks.

    A real BaseChatModel, not a stub object, because LangGraph's
    `stream_mode="messages"` works by tapping the callback events a chat model
    emits. An object that merely has an `ainvoke` method produces no token
    events, and the endpoint's streaming path would go completely untested.

    Responses cycle rather than exhaust so one instance can serve several
    requests in a test without the second one running off the end.

    A tool-calling response is emitted as a single chunk. Faking incremental
    tool-call assembly (the `tool_call_chunks` accumulation the OpenAI parser
    does) would test LangChain's parser, not this codebase.
    """

    responses: Sequence[AIMessage]
    chunk_size: int = 4

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        # Tools are already decided by the script, so binding is a no-op. This is
        # the method the built-in fakes do not implement.
        return self

    def _next_response(self, messages: list[BaseMessage]) -> AIMessage:
        if not self.responses:
            raise ValueError("ScriptedChatModel needs at least one response")
        # Count only AI turns already in the history: that is how many times this
        # model has been asked, and it survives the state being restored from a
        # checkpoint rather than held in an instance counter.
        turns = sum(1 for m in messages if isinstance(m, AIMessage))
        return self.responses[turns % len(self.responses)]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self._next_response(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._next_response(messages)

        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_calls=message.tool_calls,
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call["args"], ensure_ascii=False),
                            "id": call.get("id"),
                            "index": i,
                            "type": "tool_call_chunk",
                        }
                        for i, call in enumerate(message.tool_calls)
                    ],
                )
            )
            return

        text = message.content if isinstance(message.content, str) else ""
        step = max(1, self.chunk_size)
        for start in range(0, len(text), step):
            yield ChatGenerationChunk(message=AIMessageChunk(content=text[start : start + step]))


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> AIMessage:
    """An AIMessage that asks for one tool call."""
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
