from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from api.documents import (
    CHUNK_CHARS,
    MAX_TEXT_CHARS,
    MAX_UPLOAD_BYTES,
    document_name,
    search_tokens,
)

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
    outline TEXT NOT NULL DEFAULT '[]', outline_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(session_id, id)
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL, body TEXT NOT NULL, sources TEXT NOT NULL, citations TEXT NOT NULL,
    origin_session_id TEXT, origin_message_id TEXT, version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, origin_session_id, origin_message_id)
);
CREATE INDEX IF NOT EXISTS notes_project ON notes(project_id, updated_at);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('pdf', 'markdown')),
    status TEXT NOT NULL CHECK(status IN ('pending','processing','ready','needs_ocr','failed')),
    error TEXT, chunk_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    sha256 TEXT NOT NULL, blob_name TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project_id, sha256)
);
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, text TEXT NOT NULL, location TEXT NOT NULL,
    page INTEGER, line_start INTEGER, line_end INTEGER, UNIQUE(document_id, ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_id UNINDEXED, project_id UNINDEXED, tokens, tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_delete_fts AFTER DELETE ON chunks BEGIN
    DELETE FROM chunk_fts WHERE chunk_id = old.id;
END;
COMMIT;
"""
_DOCUMENT_FIELDS = (
    "id",
    "project_id",
    "name",
    "kind",
    "status",
    "error",
    "chunk_count",
    "created_at",
)


class WorkspaceConflict(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _title(value: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError(f"名称不能为空且不能超过 {maximum} 个字符")
    return value.strip()


def _document(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _DOCUMENT_FIELDS}


class WorkspaceStore:
    def __init__(self, path: str | Path, blob_dir: str | Path):
        self.path = Path(path).resolve()
        self.blob_dir = Path(blob_dir).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
        finally:
            connection.close()
        self.reset_interrupted_documents()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return row

    @classmethod
    def _session(cls, connection: sqlite3.Connection, project_id: str, session_id: str) -> dict:
        cls._project(connection, project_id)
        row = connection.execute(
            "SELECT id, project_id, title, updated_at FROM sessions "
            "WHERE project_id = ? AND id = ?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return dict(row)

    @classmethod
    def _document_row(
        cls, connection: sqlite3.Connection, project_id: str, document_id: str
    ) -> sqlite3.Row:
        cls._project(connection, project_id)
        row = connection.execute(
            "SELECT * FROM documents WHERE project_id = ? AND id = ?", (project_id, document_id)
        ).fetchone()
        if row is None:
            raise KeyError(document_id)
        return row

    def list_projects(self) -> list[dict]:
        with self._connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, name, created_at FROM projects ORDER BY created_at, id"
                )
            ]

    def create_project(self, name: str) -> dict:
        project = {"id": str(uuid4()), "name": _title(name), "created_at": _now()}
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
                (project["id"], project["name"], project["created_at"]),
            )
        return project

    def get_project(self, project_id: str) -> dict:
        with self._connection() as connection:
            row = self._project(connection, project_id)
            return {key: row[key] for key in ("id", "name", "created_at")}

    def rename_project(self, project_id: str, name: str) -> dict:
        with self._connection(write=True) as connection:
            row = self._project(connection, project_id)
            name = _title(name)
            connection.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
            return {"id": project_id, "name": name, "created_at": row["created_at"]}

    def list_sessions(self, project_id: str) -> list[dict]:
        with self._connection() as connection:
            self._project(connection, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id, project_id, title, updated_at FROM sessions "
                    "WHERE project_id = ? ORDER BY updated_at DESC, id",
                    (project_id,),
                )
            ]

    def create_session(self, project_id: str, title: str) -> dict:
        with self._connection(write=True) as connection:
            self._project(connection, project_id)
            session = {
                "id": str(uuid4()),
                "project_id": project_id,
                "title": _title(title),
                "updated_at": _now(),
            }
            connection.execute(
                "INSERT INTO sessions(id, project_id, title, updated_at) VALUES (?, ?, ?, ?)",
                (session["id"], project_id, session["title"], session["updated_at"]),
            )
            return session

    def get_session(self, project_id: str, session_id: str) -> dict:
        with self._connection() as connection:
            return self._session(connection, project_id, session_id)

    def rename_session(self, project_id: str, session_id: str, title: str) -> dict:
        with self._connection(write=True) as connection:
            session = self._session(connection, project_id, session_id)
            session.update(title=_title(title), updated_at=_now())
            connection.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                (session["title"], session["updated_at"], session_id, project_id),
            )
            return session

    def session_messages(self, project_id: str, session_id: str) -> list[dict]:
        with self._connection() as connection:
            self._session(connection, project_id, session_id)
            return [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT body FROM messages WHERE session_id = ? ORDER BY sequence",
                    (session_id,),
                )
            ]

    def save_message(self, project_id: str, session_id: str, message: dict) -> None:
        with self._connection(write=True) as connection:
            self._session(connection, project_id, session_id)
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                raise ValueError("消息必须包含 id")
            if not message["id"] or len(message["id"]) > 200:
                raise ValueError("消息 id 无效")
            body = json.dumps(message, ensure_ascii=False, allow_nan=False)
            connection.execute(
                "INSERT INTO messages(session_id, id, body) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id, id) DO UPDATE SET body = excluded.body",
                (session_id, message["id"], body),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )

    def session_exists_anywhere(self, session_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is not None
            )

    def get_outline(self, project_id: str) -> dict:
        with self._connection() as connection:
            project = self._project(connection, project_id)
            return {
                "sections": json.loads(project["outline"]),
                "version": project["outline_version"],
            }

    def save_outline(self, project_id: str, sections: list[dict], version: int) -> dict:
        with self._connection(write=True) as connection:
            project = self._project(connection, project_id)
            if type(version) is not int or version < 0:
                raise ValueError("大纲版本无效")
            if not isinstance(sections, list) or len(sections) > 100:
                raise ValueError("大纲最多包含 100 个章节")
            cleaned = []
            ids = set()
            for section in sections:
                if not isinstance(section, dict) or set(section) != {"id", "title"}:
                    raise ValueError("章节必须包含 id 和 title")
                identifier = section["id"]
                if (
                    not isinstance(identifier, str)
                    or not identifier.strip()
                    or len(identifier) > 100
                    or identifier in ids
                    or any(ord(char) < 32 for char in identifier)
                ):
                    raise ValueError("章节 id 必须唯一且不超过 100 个字符")
                ids.add(identifier)
                cleaned.append({"id": identifier, "title": _title(section["title"])})
            if project["outline_version"] != version:
                raise WorkspaceConflict("大纲已更新，请刷新后重试")
            connection.execute(
                "UPDATE projects SET outline = ?, outline_version = outline_version + 1 "
                "WHERE id = ?",
                (json.dumps(cleaned, ensure_ascii=False), project_id),
            )
            return {"sections": cleaned, "version": version + 1}

    @staticmethod
    def _note(row: sqlite3.Row) -> dict:
        note = {
            key: row[key]
            for key in (
                "id",
                "project_id",
                "title",
                "body",
                "origin_session_id",
                "origin_message_id",
                "version",
                "created_at",
                "updated_at",
            )
        }
        note["sources"] = json.loads(row["sources"])
        note["citations"] = json.loads(row["citations"])
        return note

    @classmethod
    def _note_row(
        cls, connection: sqlite3.Connection, project_id: str, note_id: str
    ) -> sqlite3.Row:
        cls._project(connection, project_id)
        row = connection.execute(
            "SELECT * FROM notes WHERE project_id = ? AND id = ?", (project_id, note_id)
        ).fetchone()
        if row is None:
            raise KeyError(note_id)
        return row

    @staticmethod
    def _note_body(body: str) -> str:
        if not isinstance(body, str) or len(body) > 100000:
            raise ValueError("笔记正文必须是字符串且不能超过 100000 个字符")
        return body

    @classmethod
    def _note_snapshot(cls, message: dict, project_id: str) -> tuple[str, list[dict], list[dict]]:
        parts = message.get("parts")
        if (
            message.get("role") != "assistant"
            or not isinstance(parts, list)
            or any(not isinstance(part, dict) for part in parts)
        ):
            raise ValueError("只能采纳已完成且包含正文的助手消息")
        statuses = [part.get("data") for part in parts if part.get("type") == "data-turn-status"]
        if not statuses or any(
            not isinstance(status, dict) or status.get("status") != "complete"
            for status in statuses
        ):
            raise ValueError("只能采纳已完成且包含正文的助手消息")
        texts, sources, citations = [], [], []
        for part in parts:
            kind = part.get("type")
            if kind == "text":
                text = part.get("text")
                if not isinstance(text, str):
                    raise ValueError("消息正文无效")
                texts.append(text)
            elif kind in ("data-knowledge-source", "data-citation"):
                data = part.get("data")
                if not isinstance(data, dict):
                    raise ValueError("消息来源快照无效")
                if kind == "data-knowledge-source":
                    if data.get("project_id") != project_id:
                        raise ValueError("消息来源不属于当前项目")
                    sources.append(data)
                else:
                    citations.append(data)
        body = cls._note_body("".join(texts))
        if not body.strip():
            raise ValueError("只能采纳已完成且包含正文的助手消息")
        return body, sources, citations

    def list_notes(self, project_id: str) -> list[dict]:
        with self._connection() as connection:
            self._project(connection, project_id)
            return [
                self._note(row)
                for row in connection.execute(
                    "SELECT * FROM notes WHERE project_id = ? ORDER BY updated_at DESC, id",
                    (project_id,),
                )
            ]

    def get_note(self, project_id: str, note_id: str) -> dict:
        with self._connection() as connection:
            return self._note(self._note_row(connection, project_id, note_id))

    def create_note(
        self,
        project_id: str,
        title: str,
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> dict:
        with self._connection(write=True) as connection:
            self._project(connection, project_id)
            title = _title(title)
            if (session_id is None) != (message_id is None):
                raise ValueError("必须同时提供会话 id 和消息 id")
            body, sources, citations = "", [], []
            if session_id is not None:
                if any(
                    not isinstance(value, str) or not value.strip()
                    for value in (session_id, message_id)
                ):
                    raise ValueError("会话 id 和消息 id 无效")
                self._session(connection, project_id, session_id)
                existing = connection.execute(
                    "SELECT * FROM notes WHERE project_id = ? "
                    "AND origin_session_id = ? AND origin_message_id = ?",
                    (project_id, session_id, message_id),
                ).fetchone()
                if existing is not None:
                    return self._note(existing)
                row = connection.execute(
                    "SELECT body FROM messages WHERE session_id = ? AND id = ?",
                    (session_id, message_id),
                ).fetchone()
                if row is None:
                    raise KeyError(message_id)
                body, sources, citations = self._note_snapshot(json.loads(row["body"]), project_id)
            identifier, timestamp = str(uuid4()), _now()
            connection.execute(
                "INSERT INTO notes(id, project_id, title, body, sources, citations, "
                "origin_session_id, origin_message_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    project_id,
                    title,
                    body,
                    json.dumps(sources, ensure_ascii=False, allow_nan=False),
                    json.dumps(citations, ensure_ascii=False, allow_nan=False),
                    session_id,
                    message_id,
                    timestamp,
                    timestamp,
                ),
            )
            return self._note(self._note_row(connection, project_id, identifier))

    def save_note(
        self,
        project_id: str,
        note_id: str,
        title: str,
        body: str,
        version: int,
    ) -> dict:
        with self._connection(write=True) as connection:
            note = self._note_row(connection, project_id, note_id)
            title, body = _title(title), self._note_body(body)
            if type(version) is not int or version < 0:
                raise ValueError("笔记版本无效")
            if note["version"] != version:
                raise WorkspaceConflict("笔记已更新，请刷新后重试")
            connection.execute(
                "UPDATE notes SET title = ?, body = ?, version = version + 1, updated_at = ? "
                "WHERE project_id = ? AND id = ?",
                (title, body, _now(), project_id, note_id),
            )
            return self._note(self._note_row(connection, project_id, note_id))

    def delete_note(self, project_id: str, note_id: str) -> None:
        with self._connection(write=True) as connection:
            self._note_row(connection, project_id, note_id)
            connection.execute(
                "DELETE FROM notes WHERE project_id = ? AND id = ?", (project_id, note_id)
            )

    def list_documents(self, project_id: str) -> list[dict]:
        with self._connection() as connection:
            self._project(connection, project_id)
            return [
                _document(row)
                for row in connection.execute(
                    "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at, id",
                    (project_id,),
                )
            ]

    def get_document(self, project_id: str, document_id: str) -> dict:
        with self._connection() as connection:
            return _document(self._document_row(connection, project_id, document_id))

    def create_document(self, project_id: str, filename: str, data: bytes) -> dict:
        blob_path = None
        try:
            with self._connection(write=True) as connection:
                self._project(connection, project_id)
                name, kind = document_name(filename)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("文件超过 20 MiB 限制")
                digest = hashlib.sha256(data).hexdigest()
                existing = connection.execute(
                    "SELECT * FROM documents WHERE project_id = ? AND sha256 = ?",
                    (project_id, digest),
                ).fetchone()
                if existing is not None:
                    return _document(existing)
                identifier, blob_name = str(uuid4()), uuid4().hex
                blob_path = self.blob_dir / blob_name
                with blob_path.open("xb") as output:
                    output.write(data)
                connection.execute(
                    "INSERT INTO documents(id, project_id, name, kind, status, created_at, sha256, "
                    "blob_name) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                    (identifier, project_id, name, kind, _now(), digest, blob_name),
                )
                result = _document(self._document_row(connection, project_id, identifier))
            return result
        except BaseException:
            if blob_path is not None:
                blob_path.unlink(missing_ok=True)
            raise

    def get_document_chunks(self, project_id: str, document_id: str) -> list[dict]:
        with self._connection() as connection:
            self._document_row(connection, project_id, document_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT c.id, c.document_id, d.name, c.text, c.location, c.page, c.line_start, "
                    "c.line_end FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.project_id = ? AND d.id = ? ORDER BY c.ordinal",
                    (project_id, document_id),
                )
            ]

    def read_document_file(self, project_id: str, document_id: str) -> tuple[dict, bytes]:
        # Serialize opening/reading with deletion, including on Windows.
        with self._connection(write=True) as connection:
            row = self._document_row(connection, project_id, document_id)
            try:
                with (self.blob_dir / row["blob_name"]).open("rb") as source:
                    data = source.read(MAX_UPLOAD_BYTES + 1)
            except FileNotFoundError as exc:
                raise KeyError(document_id) from exc
            if len(data) > MAX_UPLOAD_BYTES:
                raise ValueError("文件超过 20 MiB 限制")
            return _document(row), data

    def read_document_job(self, project_id: str, document_id: str, generation: int) -> bytes | None:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT blob_name FROM documents WHERE project_id = ? AND id = ? "
                "AND generation = ? AND status = 'processing'",
                (project_id, document_id, generation),
            ).fetchone()
            if row is None:
                return None
            with (self.blob_dir / row["blob_name"]).open("rb") as source:
                return source.read(MAX_UPLOAD_BYTES + 1)

    def retry_document(self, project_id: str, document_id: str) -> dict:
        with self._connection(write=True) as connection:
            self._document_row(connection, project_id, document_id)
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            connection.execute(
                "UPDATE documents SET status = 'pending', error = NULL, chunk_count = 0, "
                "generation = generation + 1 WHERE project_id = ? AND id = ?",
                (project_id, document_id),
            )
            return _document(self._document_row(connection, project_id, document_id))

    def delete_document(self, project_id: str, document_id: str) -> None:
        with self._connection(write=True) as connection:
            row = self._document_row(connection, project_id, document_id)
            connection.execute(
                "DELETE FROM documents WHERE project_id = ? AND id = ?", (project_id, document_id)
            )
        (self.blob_dir / row["blob_name"]).unlink(missing_ok=True)

    def reset_interrupted_documents(self) -> int:
        with self._connection(write=True) as connection:
            return connection.execute(
                "UPDATE documents SET status = 'failed', error = '服务重启，请重试', "
                "generation = generation + 1 WHERE status IN ('pending', 'processing')"
            ).rowcount

    def claim_document(self, project_id: str, document_id: str) -> tuple[int, str, Path] | None:
        with self._connection(write=True) as connection:
            try:
                row = self._document_row(connection, project_id, document_id)
            except KeyError:
                return None  # A queued worker may outlive deletion.
            if row["status"] != "pending":
                return None
            connection.execute(
                "UPDATE documents SET status = 'processing' WHERE project_id = ? AND id = ?",
                (project_id, document_id),
            )
            return row["generation"], row["kind"], self.blob_dir / row["blob_name"]

    def publish_document(
        self, project_id: str, document_id: str, generation: int, status: str, chunks: list[dict]
    ) -> bool:
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM documents WHERE project_id = ? AND id = ? "
                "AND generation = ? AND status = 'processing'",
                (project_id, document_id, generation),
            ).fetchone()
            if row is None:
                return False
            if status not in {"ready", "needs_ocr"} or (status == "needs_ocr" and chunks):
                raise ValueError("解析状态无效")
            if sum(len(chunk["text"]) for chunk in chunks) > MAX_TEXT_CHARS:
                raise ValueError("文档文本超过限制")
            for ordinal, chunk in enumerate(chunks):
                if not chunk["text"].strip() or len(chunk["text"]) > CHUNK_CHARS:
                    raise ValueError("文本块长度无效")
                chunk_id = str(uuid4())
                connection.execute(
                    "INSERT INTO chunks(id, document_id, ordinal, text, location, page, "
                    "line_start, line_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        document_id,
                        ordinal,
                        chunk["text"],
                        chunk["location"],
                        chunk.get("page"),
                        chunk.get("line_start"),
                        chunk.get("line_end"),
                    ),
                )
                connection.execute(
                    "INSERT INTO chunk_fts(chunk_id, project_id, tokens) VALUES (?, ?, ?)",
                    (chunk_id, project_id, " ".join(search_tokens(chunk["text"]))),
                )
            connection.execute(
                "UPDATE documents SET status = ?, error = NULL, chunk_count = ? "
                "WHERE project_id = ? AND id = ? AND generation = ?",
                (status, len(chunks), project_id, document_id, generation),
            )
            return True

    def fail_document(self, project_id: str, document_id: str, generation: int, error: str) -> bool:
        with self._connection(write=True) as connection:
            return (
                connection.execute(
                    "UPDATE documents SET status = 'failed', error = ?, chunk_count = 0 "
                    "WHERE project_id = ? AND id = ? AND generation = ? AND status = 'processing'",
                    (error[:500], project_id, document_id, generation),
                ).rowcount
                == 1
            )

    def search(
        self, project_id: str, query: str, document_ids: list[str] | None = None, limit: int = 6
    ) -> list[dict]:
        with self._connection() as connection:
            self._project(connection, project_id)
            if document_ids is not None:
                for document_id in document_ids:
                    self._document_row(connection, project_id, document_id)
            if not isinstance(query, str) or len(query) > 2000:
                raise ValueError("搜索词不能超过 2000 个字符")
            if type(limit) is not int or limit < 1:
                raise ValueError("搜索数量必须为正整数")
            tokens = list(dict.fromkeys(search_tokens(query)))[:128]
            if not tokens or document_ids == []:
                return []
            match = " OR ".join('"' + token + '"' for token in tokens)
            selection = json.dumps(document_ids) if document_ids is not None else None
            rows = connection.execute(
                "SELECT c.id, c.document_id, d.name, c.text, c.location, c.page, c.line_start, "
                "c.line_end FROM chunk_fts JOIN chunks c ON c.id = chunk_fts.chunk_id "
                "JOIN documents d ON d.id = c.document_id "
                "WHERE chunk_fts MATCH ? AND chunk_fts.project_id = ? AND d.project_id = ? "
                "AND d.status = 'ready' "
                "AND (? IS NULL OR d.id IN (SELECT value FROM json_each(?))) "
                "ORDER BY bm25(chunk_fts), d.created_at, c.ordinal, c.id LIMIT ?",
                (match, project_id, project_id, selection, selection, min(limit, 6)),
            ).fetchall()
            results = []
            for row in rows:
                chunk = dict(row)
                text = chunk["text"]
                folded = text.casefold()
                positions = [folded.find(token) for token in tokens if token in folded]
                offset = max(0, min(positions, default=0) - 100)
                chunk["text"] = ("…" if offset else "") + text[offset : offset + 450]
                if offset + 450 < len(text):
                    chunk["text"] += "…"
                results.append(chunk)
            return results
