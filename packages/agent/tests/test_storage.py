"""storage.py invariants.

Three things this file exists to protect:

* blob keys are caller-controlled, so escaping the root must be impossible
* MemoryCache TTL/LRU semantics, which Jev result caching (D4) will rely on
* the SQLite checkpointer really survives a process boundary — that is a Week 1
  exit criterion, so it belongs in CI rather than in a devlog note

The checkpoint tests are async on purpose. The gateway drives the graph with
`astream`, and a sync saver passed every one of these tests while raising
NotImplementedError on the first real request; testing through `ainvoke` is what
makes the suite exercise the path production actually takes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from paper_agent.storage import (
    LocalBlobStore,
    MemoryCache,
    make_blob_store,
    make_cache,
    make_checkpointer,
)

# --------------------------------------------------------------------------- #
# BlobStore: path traversal
# --------------------------------------------------------------------------- #

# Every one of these is a plausible attacker- or bug-supplied key. `Path.name`
# is what flattens them; these cases exist to prove it actually does.
ESCAPE_KEYS = [
    "../../escape.txt",
    "..\\..\\escape.txt",
    "/etc/passwd",
    "C:/evil.txt",
    "a/b/c.pdf",
]

# `Path.name` does NOT flatten these — it returns the dot itself, so dest would
# resolve to the root or its parent. And Win32 redirects the device names to
# hardware instead of the filesystem. Both must be rejected, not written.
REJECTED_KEYS = ["..", ".", "", "CON", "con.txt", "NUL", "com1.log", "LPT9"]


@pytest.mark.parametrize("key", ESCAPE_KEYS)
async def test_blob_key_cannot_escape_root(tmp_path: Path, key: str) -> None:
    root = tmp_path / "blobs"
    store = LocalBlobStore(root)

    dest = Path(await store.put(key, b"payload"))

    assert dest.resolve().is_relative_to(root.resolve())
    # Stronger than checking dest: nothing whatsoever may appear beside `root`.
    # A traversal that created tmp_path/escape.txt would fail here even if the
    # returned path somehow still looked contained.
    assert [p.name for p in tmp_path.iterdir()] == ["blobs"]
    assert sorted(p.name for p in root.iterdir()) == [dest.name]


@pytest.mark.parametrize("key", REJECTED_KEYS)
async def test_blob_rejects_dot_and_device_keys(tmp_path: Path, key: str) -> None:
    store = LocalBlobStore(tmp_path / "blobs")

    with pytest.raises(ValueError, match="invalid blob key"):
        await store.put(key, b"payload")
    # get() shares the contract, so a bad key cannot be used to probe paths
    with pytest.raises(ValueError, match="invalid blob key"):
        await store.get(key)


async def test_blob_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    await store.put("paper.pdf", b"%PDF-1.7 binary")
    assert await store.get("paper.pdf") == b"%PDF-1.7 binary"


async def test_put_returns_absolute_path(tmp_path: Path) -> None:
    """Guards the original bug: Path.as_uri() rejects relative paths.

    LocalBlobStore resolves its root up front so callers always get an absolute
    path back, whatever they passed in.
    """
    store = LocalBlobStore(tmp_path / "blobs")
    assert Path(await store.put("x.txt", b"1")).is_absolute()


def test_blob_root_is_created_recursively(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "deep"
    LocalBlobStore(root)
    assert root.is_dir()


# --------------------------------------------------------------------------- #
# MemoryCache
# --------------------------------------------------------------------------- #


class _FakeClock:
    """Stands in for the `time` module so TTL can be tested at its boundary."""

    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


async def test_cache_miss_returns_none() -> None:
    assert await MemoryCache().get("absent") is None


async def test_cache_hit() -> None:
    cache = MemoryCache()
    await cache.set("k", "v", ttl_seconds=60)
    assert await cache.get("k") == "v"


async def test_cache_ttl_expires_at_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _FakeClock()
    monkeypatch.setattr("paper_agent.storage.time", clock)
    cache = MemoryCache()
    await cache.set("k", "v", ttl_seconds=30)

    clock.t += 29
    assert await cache.get("k") == "v", "must still be live before the TTL elapses"

    clock.t += 2
    assert await cache.get("k") is None, "must be gone once the TTL elapses"


async def test_expired_entry_is_evicted_not_just_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired read must drop the entry, or a cache that is only ever read
    would grow without bound."""
    clock = _FakeClock()
    monkeypatch.setattr("paper_agent.storage.time", clock)
    cache = MemoryCache()
    await cache.set("k", "v", ttl_seconds=1)

    clock.t += 2
    assert await cache.get("k") is None
    assert cache._data == {}


async def test_cache_lru_evicts_least_recently_used() -> None:
    cache = MemoryCache(maxsize=2)
    await cache.set("a", "1", ttl_seconds=60)
    await cache.set("b", "2", ttl_seconds=60)
    await cache.set("c", "3", ttl_seconds=60)

    assert await cache.get("a") is None
    assert await cache.get("b") == "2"
    assert await cache.get("c") == "3"


async def test_cache_read_refreshes_recency() -> None:
    """A get() must count as use. Without move_to_end on read this evicts `a`
    instead of `b`, which is a silent correctness bug under real traffic."""
    cache = MemoryCache(maxsize=2)
    await cache.set("a", "1", ttl_seconds=60)
    await cache.set("b", "2", ttl_seconds=60)

    assert await cache.get("a") == "1"
    await cache.set("c", "3", ttl_seconds=60)

    assert await cache.get("a") == "1"
    assert await cache.get("b") is None


async def test_cache_overwrite_does_not_grow() -> None:
    cache = MemoryCache(maxsize=2)
    for _ in range(5):
        await cache.set("same", "v", ttl_seconds=60)
    assert len(cache._data) == 1


# --------------------------------------------------------------------------- #
# Factories: selection is by env var, so a typo must fail loudly
# --------------------------------------------------------------------------- #


def test_make_cache_memory() -> None:
    assert isinstance(make_cache("memory", ""), MemoryCache)


def test_make_cache_unknown_backend() -> None:
    with pytest.raises(ValueError, match="CACHE_BACKEND"):
        make_cache("memcached", "")


async def test_make_checkpointer_memory() -> None:
    assert isinstance(await make_checkpointer("memory", "", ""), InMemorySaver)


async def test_make_checkpointer_unknown_backend() -> None:
    with pytest.raises(ValueError, match="CHECKPOINT_BACKEND"):
        await make_checkpointer("mysql", "", "")


async def test_make_checkpointer_sqlite_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "cp.sqlite"
    async with _saver("sqlite", str(path)) as saver:
        assert isinstance(saver, AsyncSqliteSaver)
    assert path.parent.is_dir()


async def test_make_checkpointer_postgres_is_an_explicit_gap() -> None:
    """Same shape as BLOB_BACKEND=s3: the production backend is a Week 4 task and
    must fail with instructions, not with an ImportError from a package that was
    never declared as a dependency."""
    with pytest.raises(NotImplementedError, match="AsyncPostgresSaver"):
        await make_checkpointer("postgres", "", "postgresql://localhost/agent")


def test_make_blob_store_local(tmp_path: Path) -> None:
    assert isinstance(make_blob_store("local", str(tmp_path), {}), LocalBlobStore)


def test_make_blob_store_s3_is_an_explicit_gap(tmp_path: Path) -> None:
    """Deliberately unimplemented. The message must say what to do instead,
    because BLOB_BACKEND=s3 is a value someone will try in Week 3."""
    with pytest.raises(NotImplementedError, match="BLOB_BACKEND=local"):
        make_blob_store("s3", str(tmp_path), {})


def test_make_blob_store_unknown_backend(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BLOB_BACKEND"):
        make_blob_store("gcs", str(tmp_path), {})


# --------------------------------------------------------------------------- #
# Checkpoint durability — the Week 1 exit criterion, as a regression test
# --------------------------------------------------------------------------- #


class _State(TypedDict):
    value: str


def _echo(_state: _State) -> dict:
    return {"value": "pong"}


def _build(saver):
    graph = StateGraph(_State)
    graph.add_node("echo", _echo)
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)
    return graph.compile(checkpointer=saver)


@asynccontextmanager
async def _saver(backend: str, path: str = "") -> AsyncIterator[Any]:
    """A checkpointer whose connection is closed afterwards.

    Mirrors the lifespan in apps/api. Without the close, every test leaves an
    aiosqlite thread behind and the suite grows one thread per checkpoint test.
    """
    saver = await make_checkpointer(backend, path, "")
    try:
        yield saver
    finally:
        conn = getattr(saver, "conn", None)
        if conn is not None:
            await conn.close()


@asynccontextmanager
async def _graph(backend: str, path: str = "") -> AsyncIterator[Any]:
    async with _saver(backend, path) as saver:
        yield _build(saver)


async def test_sqlite_checkpoint_survives_a_new_saver_instance(tmp_path: Path) -> None:
    """Restart the backend, conversation state is not lost.

    A second saver over the same file stands in for a restarted process. If this
    passes the state genuinely reached disk instead of living in memory — which
    is the whole difference between CHECKPOINT_BACKEND=memory and =sqlite.
    """
    path = str(tmp_path / "cp.sqlite")
    config = {"configurable": {"thread_id": "t-roundtrip"}}

    async with _graph("sqlite", path) as app:
        result = await app.ainvoke({"value": "ping"}, config)
    assert result["value"] == "pong"

    async with _graph("sqlite", path) as app:
        snapshot = await app.aget_state(config)
    assert snapshot.values["value"] == "pong"
    assert not snapshot.next, "graph should have run to completion"


async def test_memory_checkpoint_does_not_survive_a_new_instance() -> None:
    """The contrast case. Proves the test above is measuring durability and not
    just that ainvoke() returned something."""
    config = {"configurable": {"thread_id": "t-memory"}}
    async with _graph("memory") as app:
        await app.ainvoke({"value": "ping"}, config)

    async with _graph("memory") as app:
        assert not (await app.aget_state(config)).values


async def test_threads_are_isolated(tmp_path: Path) -> None:
    """One SQLite file holds many conversations; thread_id is the only thing
    separating them. A leak here means users seeing each other's state."""
    path = str(tmp_path / "cp.sqlite")
    async with _graph("sqlite", path) as app:
        await app.ainvoke({"value": "ping"}, {"configurable": {"thread_id": "a"}})

    async with _graph("sqlite", path) as app:
        a = await app.aget_state({"configurable": {"thread_id": "a"}})
        b = await app.aget_state({"configurable": {"thread_id": "b"}})
    assert a.values["value"] == "pong"
    assert not b.values


async def test_unknown_thread_returns_empty_not_error(tmp_path: Path) -> None:
    """A first message on a brand-new thread_id must not 500. This is the read
    the gateway makes before deciding whether to seed from client history."""
    async with _graph("sqlite", str(tmp_path / "cp.sqlite")) as app:
        snapshot = await app.aget_state({"configurable": {"thread_id": "never-seen"}})
    assert not snapshot.values


async def test_sqlite_saver_streams(tmp_path: Path) -> None:
    """`astream` is the call the gateway actually makes, and it is the one that
    raised NotImplementedError with the sync SqliteSaver — after startup, on the
    first request, which is the worst possible time to find out.
    """
    config = {"configurable": {"thread_id": "t-stream"}}
    async with _graph("sqlite", str(tmp_path / "cp.sqlite")) as app:
        chunks = [c async for c in app.astream({"value": "ping"}, config)]
    assert chunks == [{"echo": {"value": "pong"}}]
