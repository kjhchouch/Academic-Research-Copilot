"""Bridge from an MCP server process to LangChain tools.

Why this file exists instead of a dependency
-------------------------------------------
The obvious package, `langchain-mcp-adapters`, pins `mcp<2.0.0,>=1.24.0` (checked
on PyPI: still true at 0.3.2, 2026-08-06, and the repository now says it is no
longer actively maintained). Installing it would downgrade `mcp` to 1.x, and
1.x has neither `mcp.server.mcpserver.MCPServer` nor `mcp.client.Client` -- so it
would break the D5 server to talk to the D5 server. LangChain's own replacement,
native `langchain.mcp` in langchain 1.4, reaches MCP through `fastmcp>=4,<5`: a
second abstraction layer plus the whole `langchain` distribution, where this
project deliberately depends only on `langchain-core` and `langgraph`.

What is left is the client that ships inside the SDK we already depend on. It is
about a hundred lines to adapt, and every behaviour it relies on was measured
rather than remembered; the measurements are in the comments below and in
docs/devlog.md, D6.

Two rules this module enforces
------------------------------
**Tool results are data.** They come from a process that talks to the internet,
so they are untrusted. Server-authored error text is passed to the model, because
that text was written for it. Transport-level exception text is not: it goes to
the log and the model gets a generic failure. An httpx error string can contain a
request URL, and the model's context is not a place to put one.

**The server's `instructions` are not injected.** MCP clients conventionally
splice `initialize.result.instructions` into the system prompt. Ours does not.
The system prompt is this product's policy layer -- the "辅助不代写" boundary
lives there -- and letting a connected process rewrite it means any future
third-party server could too. `instructions` stays available on the client for
whoever wants to read it; nothing here forwards it to a model.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger(__name__)


class MCPBridgeError(RuntimeError):
    """The server could not be spawned, or did not answer `tools/list`."""


@dataclass(frozen=True, slots=True)
class StdioServerSpec:
    """How to start one MCP server, and what it is allowed to know.

    `env` is the child's whole environment beyond a short list of platform
    variables. That is a security property worth stating plainly: `stdio_client`
    builds the child's environment as `get_default_environment() | (env or {})`,
    and `get_default_environment()` returns only twelve inherited names on
    Windows -- PATH, SYSTEMROOT, TEMP and friends. Nothing else crosses unless it
    is written here, so the LLM and Langfuse keys this process holds stay in this
    process. `tests/test_tools_mcp.py` asserts the forwarding is that narrow.
    """

    name: str
    command: str
    args: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    #: Per-request read timeout, in seconds. Left as None to use the SDK default;
    #: the literature server bounds its own upstream calls at
    #: LITERATURE_TIMEOUT_SECONDS, so a second bound here would mostly hide which
    #: of the two fired.
    read_timeout_seconds: float | None = None


#: Seam for tests. Production passes None and gets `Client(StdioServerParameters)`,
#: which spawns a subprocess. Tests pass a factory returning
#: `Client(an_in_process_MCPServer)`, which is the same code path from the
#: handshake onwards -- `Client.__init__` accepts either -- minus the process.
#: Same reasoning as the `transport` seam on D5's upstream clients.
type ClientFactory = Callable[[StdioServerParameters], Any]


def _text_of(result: Any) -> str:
    """All text blocks of a result, joined.

    Filters on `type == "text"` rather than taking `content[0]`: a tool may return
    images or embedded resources alongside, and MCP does not promise text first.
    """
    return "\n".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


class MCPToolBridge:
    """One long-lived connection to one MCP server, exposed as LangChain tools.

    Lifecycle is `await connect()` once at startup, `await aclose()` once at
    shutdown, and `tools` in between. It is entered and closed from the same task
    (FastAPI's lifespan) and *used* from any task: `Client` is an async context
    manager whose exit unwinds an anyio task group, so the entry and exit have to
    be in one task, but a `call_tool` from a request handler is only a message on
    a stream. Measured -- three concurrent calls from three different tasks all
    returned correctly, and eight from one task took 8ms.

    The connection is not self-healing. If the child dies, every call after that
    raises `MCPError("Connection closed")`, which this class turns into an error
    result and logs. Restarting a crashed process is a supervisor's job (Week 4
    deployment), not a client's; a client that quietly respawns its server also
    quietly hides the crash.
    """

    def __init__(
        self, spec: StdioServerSpec, *, client_factory: ClientFactory | None = None
    ) -> None:
        self._spec = spec
        self._factory = client_factory
        self._stack = contextlib.AsyncExitStack()
        self._client: Any | None = None
        self._tools: list[StructuredTool] = []

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def tools(self) -> list[StructuredTool]:
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    @property
    def connected(self) -> bool:
        return self._client is not None

    def describe(self) -> dict[str, Any]:
        """A presence-and-names summary for /health. No env, no URLs, no keys."""
        return {
            "server": self._spec.name,
            "connected": self.connected,
            "tools": self.tool_names,
        }

    def _make_client(self, params: StdioServerParameters) -> Any:
        if self._factory is not None:
            return self._factory(params)
        return Client(params, read_timeout_seconds=self._spec.read_timeout_seconds)

    async def connect(self) -> MCPToolBridge:
        """Spawn the server, handshake, and build one LangChain tool per MCP tool.

        Raises MCPBridgeError rather than letting the original exception through:
        a spawn failure carries the interpreter's view of a path or an ImportError
        from the child, which belongs in a log, not in a startup traceback the
        caller has to interpret. The cause is chained, so nothing is lost.
        """
        if self._client is not None:
            raise MCPBridgeError(f"{self._spec.name} is already connected")

        params = StdioServerParameters(
            command=self._spec.command,
            args=list(self._spec.args),
            # `or None` because an empty dict and None mean the same thing to
            # stdio_client, and passing {} reads like "deliberately empty env".
            env=dict(self._spec.env) or None,
            cwd=self._spec.cwd,
        )
        try:
            client = await self._stack.enter_async_context(self._make_client(params))
            listed = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 - any failure here means "no tools"
            logger.exception("could not start MCP server %s", self._spec.name)
            await self.aclose()
            raise MCPBridgeError(
                f"MCP server {self._spec.name} did not come up ({type(exc).__name__})"
            ) from exc

        self._client = client
        self._tools = [self._wrap(tool) for tool in listed.tools]
        logger.info(
            "MCP server %s connected: %d tool(s) %s",
            self._spec.name,
            len(self._tools),
            self.tool_names,
        )
        return self

    def _wrap(self, tool: Any) -> StructuredTool:
        """One MCP tool as one LangChain tool.

        `args_schema` takes the MCP `inputSchema` dict unchanged. Verified:
        `StructuredTool.args_schema` is typed `type[BaseModel] | ... | dict[str,
        Any]`, `convert_to_openai_tool` turns it into a clean function schema, and
        the coroutine is called with `**kwargs`.

        One consequence to know about: with a dict schema LangChain cannot
        validate arguments, so it logs "Tool 'x' rejected arguments: [...]" and
        passes them through anyway. Validation therefore happens once, in the
        server, which answers with `is_error` and a message naming the field. That
        is the arrangement we want -- the server owns its own contract -- but it
        does mean the log line is noise and not a bug.

        Coroutine only, no `func`. The gateway streams with `astream`; a
        synchronous `.invoke()` of a graph holding these tools raises
        NotImplementedError from langchain-core, which is a clear enough answer to
        "this tool needs an event loop".
        """
        name = tool.name

        async def call(**arguments: Any) -> Any:
            return await self._call(name, arguments)

        return StructuredTool(
            name=name,
            # An MCP tool without a description would otherwise reach the model as
            # an empty string, which reads as "no idea what this does" and invites
            # a guess. The name is a worse but honest fallback.
            description=tool.description or name,
            coroutine=call,
            args_schema=tool.input_schema,
        )

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one tool. Never raises -- a tool failure is a result, not a crash.

        That is load-bearing rather than defensive. `ToolNode`'s default handler
        absorbs only its own `ToolInvocationError` and re-raises everything else,
        so an exception from here would leave the graph, reach chat.py's
        `_public_error`, and end the SSE stream with an `error` event: one bad
        lookup would kill the whole turn instead of giving the model a chance to
        try a different identifier. Measured, with a tool that raises RuntimeError.
        """
        client = self._client
        if client is None:
            logger.warning("MCP tool %s called with no connection to %s", name, self._spec.name)
            return {"error": f"the {self._spec.name} tool server is not connected"}

        try:
            result = await client.call_tool(name, arguments)
        except Exception:  # noqa: BLE001 - the model gets a fact, the log gets the detail
            # Deliberately not str(exc). This branch is transport and process
            # failures -- a dead child ("Connection closed"), a closed session, a
            # read timeout -- and their text can carry hostnames, ports and paths.
            logger.exception("MCP tool %s on %s failed", name, self._spec.name)
            return {"error": f"tool {name} is unavailable right now; the failure was logged"}

        if result.is_error:
            # Server-authored text, written for a model to read: the D5 tools put
            # the accepted identifier shapes and the missing-key advice in here.
            # Forwarding it verbatim is what lets the agent recover on its own.
            return {"error": _text_of(result)}

        structured = result.structured_content
        if isinstance(structured, dict):
            # Returned as a dict, not as the text block that carries the same data.
            # LangChain JSON-serializes a dict return into ToolMessage.content, and
            # chat.py's `_result_of` parses that back into a JSON value on the SSE
            # wire -- so the frontend can build the references panel from a real
            # object instead of regexing prose out of a string.
            return structured
        return _text_of(result)

    async def aclose(self) -> None:
        """Drop the connection and let the child go. Safe to call twice."""
        self._client = None
        self._tools = []
        # Swapped out before being closed, so a second call -- or a call racing
        # this one -- sees an empty stack rather than unwinding the same resources
        # twice.
        stack, self._stack = self._stack, contextlib.AsyncExitStack()
        try:
            await stack.aclose()
        except Exception:  # noqa: BLE001 - shutdown must not mask the original error
            # Reached when the child already died: stdio_client's teardown then
            # surfaces an ExceptionGroup from its task group. Logged, not raised --
            # this runs in lifespan shutdown, where raising would replace whatever
            # error actually stopped the app.
            logger.exception("error while closing MCP server %s", self._spec.name)


def assert_unique_tool_names(tools: Sequence[Any]) -> None:
    """Fail at composition time if two tools share a name.

    `ToolNode` indexes tools into a dict by name, so a duplicate does not error --
    the last one silently wins and the other becomes unreachable while still being
    advertised to the model, which then gets "unknown tool" back. D5 already paid
    for one shadowing bug of exactly this shape (`format_citation` calling itself),
    and now that tools arrive from two independent sources the collision is a
    configuration mistake rather than a typo.
    """
    seen: dict[str, int] = {}
    for tool in tools:
        seen[tool.name] = seen.get(tool.name, 0) + 1
    duplicates = sorted(name for name, count in seen.items() if count > 1)
    if duplicates:
        raise MCPBridgeError(f"duplicate tool name(s): {', '.join(duplicates)}")
