"""stdout purity, against a real subprocess.

The Week 1 plan names this the biggest trap of D5: a stdio MCP server's stdout *is*
the JSON-RPC channel, so one stray `print()` and the client sees an opaque parse
error instead of a tool result. Every other test here runs the server in process,
which cannot catch that -- there is no pipe to corrupt. So this module spawns the
thing the way D6 will spawn it and asserts the property directly: **every line the
child writes to stdout parses as JSON-RPC**, and the logs are on stderr.

The upstream is a local `http.server` on 127.0.0.1, pointed at by `OPENALEX_BASE_URL`
and answering from the real captured fixture. No network, no API key, no budget.

Slow relative to the rest of the suite -- it pays for an interpreter start. Worth it:
this is the only test of the artefact that actually ships.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

READ_TIMEOUT_SECONDS = 30.0
DRAIN_SECONDS = 0.3


class _Upstream(BaseHTTPRequestHandler):
    """Answers every GET with one canned body, so the child's HTTP layer is real."""

    payload: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches on
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence the access log; it would otherwise land in this process's stderr."""


class _Child:
    """The spawned server plus everything it has written, split by stream."""

    def __init__(self, process: subprocess.Popen[str], lines: queue.Queue[tuple[str, str | None]]):
        self.process = process
        self._lines = lines
        self._eof: set[str] = set()
        self.stdout: list[str] = []
        self.stderr: list[str] = []

    def send(self, payload: dict[str, Any]) -> None:
        stream = self.process.stdin
        assert stream is not None
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()

    def response(self, request_id: int) -> dict[str, Any]:
        """Collect output until the response with this id arrives."""
        while True:
            tag, line = self._next_line()
            if tag == "stderr":
                self.stderr.append(line)
                continue
            self.stdout.append(line)
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def drain(self, seconds: float = DRAIN_SECONDS) -> None:
        """Absorb whatever else is in flight, so the purity assertions see it too."""
        while True:
            try:
                tag, line = self._lines.get(timeout=seconds)
            except queue.Empty:
                return
            if line is None:
                self._eof.add(tag)
                continue
            (self.stderr if tag == "stderr" else self.stdout).append(line)

    def _next_line(self) -> tuple[str, str]:
        while True:
            try:
                tag, line = self._lines.get(timeout=READ_TIMEOUT_SECONDS)
            except queue.Empty:
                raise AssertionError(
                    f"no output within {READ_TIMEOUT_SECONDS}s; "
                    f"stderr so far: {''.join(self.stderr)[-800:]}"
                ) from None
            if line is not None:
                return tag, line
            self._eof.add(tag)
            if len(self._eof) == 2:
                raise AssertionError(
                    "the server closed both streams without answering; "
                    f"stderr: {''.join(self.stderr)[-800:]}"
                )


@contextmanager
def _pumped(process: subprocess.Popen[str]) -> Iterator[_Child]:
    """Read both pipes on their own threads; a full pipe would deadlock the child."""
    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def pump(stream: Any, tag: str) -> None:
        for line in stream:
            lines.put((tag, line))
        lines.put((tag, None))

    for stream, tag in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        threading.Thread(target=pump, args=(stream, tag), daemon=True).start()
    try:
        yield _Child(process, lines)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - Windows mostly cooperates
            process.kill()


@pytest.fixture
def spawned(search_payload):
    """Spawn the real server against a local upstream serving the real fixture."""

    @contextmanager
    def launch() -> Iterator[_Child]:
        _Upstream.payload = search_payload
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        port = httpd.socket.getsockname()[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        env = dict(os.environ)
        env["OPENALEX_BASE_URL"] = f"http://127.0.0.1:{port}"
        # Blanked rather than inherited: `Settings.from_env` treats an empty value as
        # absent, so the child runs in the default keyless configuration no matter
        # what happens to be exported in the shell that ran pytest.
        env["OPENALEX_API_KEY"] = ""
        env["SEMANTIC_SCHOLAR_API_KEY"] = ""

        process = subprocess.Popen(
            [sys.executable, "-m", "mcp_literature_search"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # `errors="replace"` is for stderr, whose encoding on Windows follows the
            # console codepage. stdout is separately asserted to contain no
            # replacement character, so leniency here cannot mask a wire problem.
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        try:
            with _pumped(process) as child:
                yield child
        finally:
            httpd.shutdown()
            httpd.server_close()

    return launch


def handshake(child: _Child) -> dict[str, Any]:
    child.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "stdio-purity-test", "version": "0"},
            },
        }
    )
    result = child.response(1)
    child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return result


def call_tool(
    child: _Child, request_id: int, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    child.send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    return child.response(request_id)


def assert_stdout_is_pure(child: _Child) -> None:
    """The whole reason this file exists."""
    assert child.stdout, "the server wrote nothing to stdout"
    for line in child.stdout:
        assert "\ufffd" not in line, f"stdout is not valid UTF-8: {line!r}"
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"stdout carried a line that is not JSON: {line!r}") from exc
        assert message.get("jsonrpc") == "2.0", f"not a JSON-RPC message: {line!r}"


# ---------------------------------------------------------------------------


def test_stdout_carries_only_json_rpc_and_the_logs_go_to_stderr(spawned) -> None:
    with spawned() as child:
        initialized = handshake(child)
        child.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing = child.response(2)
        child.drain()

    assert_stdout_is_pure(child)
    assert initialized["result"]["serverInfo"]["name"] == "literature-search"
    assert [tool["name"] for tool in listing["result"]["tools"]] == [
        "search_papers",
        "get_paper",
        "format_citation",
    ]

    stderr = "".join(child.stderr)
    assert "literature-search 0.1.0 starting" in stderr
    # The startup line is exactly what would corrupt the channel if it leaked.
    wire = "".join(child.stdout)
    assert "starting" not in wire
    assert "INFO" not in wire
    assert "127.0.0.1" not in wire


def test_the_trust_boundary_travels_in_the_handshake(spawned) -> None:
    """Instructions arrive on connect, before any tool result, so they are always read."""
    with spawned() as child:
        initialized = handshake(child)
        child.drain()

    assert_stdout_is_pure(child)
    instructions = initialized["result"]["instructions"]
    assert "不可信" in instructions
    assert "不是给你的指令" in instructions
    assert "不代写" in instructions


def test_the_chinese_descriptions_survive_the_wire(spawned) -> None:
    """This console's codepage is GBK; the SDK re-wraps fd 1 as UTF-8."""
    with spawned() as child:
        handshake(child)
        child.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing = child.response(2)
        child.drain()

    assert_stdout_is_pure(child)
    tool = listing["result"]["tools"][0]
    assert tool["name"] == "search_papers"
    assert "检索学术论文" in tool["description"]
    assert "不要重试" in tool["description"]
    schema = json.dumps(tool["inputSchema"], ensure_ascii=False)
    assert "检索主题" in schema


def test_search_papers_over_stdio_returns_real_records_with_valid_dois(spawned) -> None:
    """The offline form of the D5 acceptance criterion, end to end through a pipe."""
    with spawned() as child:
        handshake(child)
        called = call_tool(child, 2, "search_papers", {"query": "contrastive learning", "limit": 5})
        child.drain()

    assert_stdout_is_pure(child)
    result = called["result"]
    assert result["isError"] is False
    paper = result["structuredContent"]["papers"][0]
    assert paper["title"] == "Contrastive Learning for Sequential Recommendation"
    assert paper["doi"].startswith("10.")
    assert paper["year"] == 2022


def test_a_bad_call_returns_an_error_and_leaves_the_stream_usable(spawned) -> None:
    """One failure must not cost the connection: a stdio server has only the one."""
    with spawned() as child:
        handshake(child)
        failed = call_tool(child, 2, "get_paper", {"paper_id": "nonsense"})
        child.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        listing = child.response(3)
        child.drain()

    assert_stdout_is_pure(child)
    assert failed["result"]["isError"] is True
    assert "OpenAlex ID" in failed["result"]["content"][0]["text"]
    assert len(listing["result"]["tools"]) == 3
