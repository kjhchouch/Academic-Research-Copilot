"""Storage backends with a local-first default.

The whole app must boot with zero external services. Every backend has a
local implementation (SQLite / in-process / filesystem) and a production one
(Postgres / Redis / S3). Selection is by env var; call sites depend only on
the returned object, so swapping never touches business logic.

    CHECKPOINT_BACKEND=sqlite     # sqlite | memory | postgres
    CACHE_BACKEND=memory          # memory | redis
    BLOB_BACKEND=local            # local | s3

Why this matters for the schedule: `docker compose up` is a Week 4 deployment
concern, not a Week 1 prerequisite.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Checkpointing (LangGraph)
# --------------------------------------------------------------------------- #


def make_checkpointer(backend: str, sqlite_path: str, database_url: str) -> Any:
    """Return a LangGraph checkpointer.

    D3 uses `memory`; D7 switches to `sqlite` (or `postgres` once Docker or a
    native Postgres with the right extensions is available).
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    if backend == "sqlite":
        # sync driver: no extra event-loop plumbing, fine for single-user dev.
        from langgraph.checkpoint.sqlite import SqliteSaver

        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        return SqliteSaver(conn)

    if backend == "postgres":
        from langgraph.checkpoint.postgres import PostgresSaver

        # PostgresSaver needs psycopg v3, NOT psycopg2.
        cm = PostgresSaver.from_conn_string(database_url)
        saver = cm.__enter__()
        saver.setup()
        return saver

    raise ValueError(f"unknown CHECKPOINT_BACKEND: {backend!r}")


# --------------------------------------------------------------------------- #
# Cache (Jev decision results, D4)
# --------------------------------------------------------------------------- #


class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class MemoryCache:
    """LRU + TTL, single process. Good enough until there is more than one worker."""

    def __init__(self, maxsize: int = 4096) -> None:
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._maxsize = maxsize

    async def get(self, key: str) -> str | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        value, expires_at = hit
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl_seconds)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)


def make_cache(backend: str, redis_url: str) -> Cache:
    if backend == "memory":
        return MemoryCache()
    if backend == "redis":
        import redis.asyncio as aioredis

        return _RedisCache(aioredis.from_url(redis_url, decode_responses=True))
    raise ValueError(f"unknown CACHE_BACKEND: {backend!r}")


class _RedisCache:
    def __init__(self, client: Any) -> None:
        self._c = client

    async def get(self, key: str) -> str | None:
        return await self._c.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._c.set(key, value, ex=ttl_seconds)


# --------------------------------------------------------------------------- #
# Blobs (uploaded PDFs, Week 3)
# --------------------------------------------------------------------------- #


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> str: ...
    async def get(self, key: str) -> bytes: ...


# Win32 reserves these regardless of extension or directory, and its path parser
# redirects them to a device instead of the filesystem. Opening "CON" for write
# attaches to the console, which under a service with no console attached can
# hang the request rather than fail it.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _safe_name(key: str) -> str:
    """Flatten a caller-controlled key to one filename guaranteed inside root.

    `Path(key).name` alone is not enough. For "." and ".." it returns the dot
    itself, so dest resolves to the root or its parent and the call dies deep
    inside write_bytes with a PermissionError that says nothing about the key.
    Reject those up front with a message that names the offending input.
    """
    name = Path(key).name
    if name in {"", ".", ".."}:
        raise ValueError(f"invalid blob key: {key!r}")
    if name.lower().split(".")[0] in _WINDOWS_RESERVED:
        raise ValueError(f"invalid blob key (reserved device name): {key!r}")
    return name


class LocalBlobStore:
    def __init__(self, root: str | Path) -> None:
        # resolve() up front: as_uri() rejects relative paths, and we want a
        # stable absolute prefix to guarantee keys cannot escape the root.
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes) -> str:
        # Keys come from user-uploaded filenames, so flattening is mandatory:
        # "a/b/c.pdf" and "../../escape.txt" both become the bare basename.
        dest = self._root / _safe_name(key)
        dest.write_bytes(data)
        return str(dest)

    async def get(self, key: str) -> bytes:
        return (self._root / _safe_name(key)).read_bytes()


def make_blob_store(backend: str, storage_dir: str, s3: dict[str, str]) -> BlobStore:
    if backend == "local":
        return LocalBlobStore(storage_dir)
    if backend == "s3":
        raise NotImplementedError("S3BlobStore is a Week 3 task; use BLOB_BACKEND=local for now")
    raise ValueError(f"unknown BLOB_BACKEND: {backend!r}")
