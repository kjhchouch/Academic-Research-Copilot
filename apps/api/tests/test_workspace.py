from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from api.documents import (
    CHUNK_CHARS,
    MAX_UPLOAD_BYTES,
    DocumentError,
    _process_document,
    parse_document,
    parse_markdown,
    search_tokens,
)
from api.routes import workspace
from api.workspace_store import WorkspaceConflict, WorkspaceStore
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from api import documents


@pytest.fixture
def store(tmp_path: Path) -> WorkspaceStore:
    return WorkspaceStore(tmp_path / "db" / "workspace.sqlite3", tmp_path / "blobs")


@pytest.fixture
def projects(store: WorkspaceStore) -> tuple[str, str]:
    return store.create_project("甲项目")["id"], store.create_project("乙项目")["id"]


@pytest.fixture
def client(store: WorkspaceStore):
    app = FastAPI()
    app.state.workspace = store
    app.include_router(workspace.router)
    with TestClient(app) as test_client:
        yield test_client


def ingest(store: WorkspaceStore, project_id: str, text: str, name: str = "paper.md") -> dict:
    document = store.create_document(project_id, name, text.encode())
    _process_document(store, project_id, document["id"])
    return store.get_document(project_id, document["id"])


def pdf_bytes(text: str | None = "Research evidence", pages: int = 1, password: str | None = None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if text is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_projects_sessions_messages_persist_and_upsert(store: WorkspaceStore, projects):
    first, second = projects
    assert store.path.exists() and store.blob_dir.is_dir()
    assert len(store.list_projects()) == 2
    assert store.rename_project(first, "  研究项目  ")["name"] == "研究项目"
    session = store.create_session(first, "  文献综述  ")
    assert session["title"] == "文献综述"
    assert set(session) == {"id", "project_id", "title", "updated_at"}
    assert store.session_exists_anywhere(session["id"])
    assert not store.session_exists_anywhere("missing")
    message = {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "初稿"}]}
    other = {"id": "m2", "role": "assistant", "parts": [{"type": "text", "text": "答复"}]}
    store.save_message(first, session["id"], message)
    store.save_message(first, session["id"], other)
    message["parts"][0]["text"] = "修改后"
    store.save_message(first, session["id"], message)
    assert store.session_messages(first, session["id"]) == [message, other]
    assert store.rename_session(first, session["id"], "  Results ")["title"] == "Results"
    assert store.list_sessions(second) == []
    other_session = store.create_session(second, "Other")
    store.save_message(second, other_session["id"], other)
    reopened = WorkspaceStore(store.path, store.blob_dir)
    assert reopened.get_project(first)["name"] == "研究项目"
    assert reopened.get_session(first, session["id"])["title"] == "Results"
    assert reopened.session_messages(first, session["id"]) == [message, other]
    assert reopened.session_messages(second, other_session["id"]) == [other]


@pytest.mark.parametrize("name", ["", "   ", "x" * 201, None])
def test_invalid_project_names(store, name):
    with pytest.raises(ValueError):
        store.create_project(name)
    assert store.list_projects() == []


def test_missing_project_and_cross_project_operations(store, projects):
    first, second = projects
    session = store.create_session(first, "Session")["id"]
    document = ingest(store, first, "Research evidence")["id"]
    for scope in (second, "missing"):
        operations = [
            lambda scope=scope: store.get_session(scope, session),
            lambda scope=scope: store.rename_session(scope, session, "Other"),
            lambda scope=scope: store.session_messages(scope, session),
            lambda scope=scope: store.save_message(scope, session, {"id": "m1"}),
            lambda scope=scope: store.get_document(scope, document),
            lambda scope=scope: store.get_document_chunks(scope, document),
            lambda scope=scope: store.read_document_file(scope, document),
            lambda scope=scope: store.retry_document(scope, document),
            lambda scope=scope: store.delete_document(scope, document),
            lambda scope=scope: store.search(scope, "Research", [document]),
            lambda scope=scope: store.search(scope, "", [document]),
        ]
        for operation in operations:
            with pytest.raises(KeyError):
                operation()
    for operation in (
        lambda: store.get_project("missing"),
        lambda: store.rename_project("missing", "Name"),
        lambda: store.list_sessions("missing"),
        lambda: store.create_session("missing", "Title"),
        lambda: store.list_documents("missing"),
        lambda: store.create_document("missing", "x.md", b"data"),
        lambda: store.get_outline("missing"),
        lambda: store.save_outline("missing", [], 0),
        lambda: store.search("missing", ""),
    ):
        with pytest.raises(KeyError):
            operation()
    assert store.get_document(first, document)["status"] == "ready"


def test_outline_validation_versioning_and_isolation(store, projects):
    first, second = projects
    assert store.get_outline(first) == {"sections": [], "version": 0}
    sections = [{"id": "intro", "title": "  Introduction "}]
    saved = store.save_outline(first, sections, 0)
    assert saved == {"sections": [{"id": "intro", "title": "Introduction"}], "version": 1}
    assert store.get_outline(second) == {"sections": [], "version": 0}
    with pytest.raises(WorkspaceConflict):
        store.save_outline(first, [], 0)
    assert store.get_outline(first) == saved
    assert store.save_outline(first, [], 1) == {"sections": [], "version": 2}
    assert WorkspaceStore(store.path, store.blob_dir).get_outline(first)["version"] == 2


@pytest.mark.parametrize(
    "sections,version",
    [
        ([{"id": "same", "title": "One"}, {"id": "same", "title": "Two"}], 0),
        ([{"id": "", "title": "One"}], 0),
        ([{"id": " ", "title": "One"}], 0),
        ([{"id": "x" * 101, "title": "One"}], 0),
        ([{"id": "x", "title": " "}], 0),
        ([{"id": "x", "title": "x" * 201}], 0),
        ([{"id": str(index), "title": "Title"} for index in range(101)], 0),
        ([{"id": "x", "title": "X", "extra": 1}], 0),
        ([], -1),
        ([], True),
        ([], "0"),
    ],
)
def test_outline_rejects_invalid_sections(store, projects, sections, version):
    with pytest.raises(ValueError):
        store.save_outline(projects[0], sections, version)
    assert store.get_outline(projects[0])["version"] == 0


def test_concurrent_outline_writes_have_one_winner(store, projects):
    def save(title):
        try:
            store.save_outline(projects[0], [{"id": "a", "title": title}], 0)
            return "saved"
        except WorkspaceConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["First", "Second"]))
    assert sorted(results) == ["conflict", "saved"]
    assert store.get_outline(projects[0])["version"] == 1


def test_markdown_chunks_preserve_heading_lines_and_bounded_text():
    chunks = parse_markdown("# 研究\n中文证据\n\n## Methods\nrandomized trial\n".encode())
    assert [chunk["text"] for chunk in chunks] == [
        "# 研究\n中文证据",
        "## Methods\nrandomized trial",
    ]
    assert (chunks[0]["line_start"], chunks[0]["line_end"]) == (1, 3)
    assert (chunks[1]["line_start"], chunks[1]["line_end"]) == (4, 5)
    assert chunks[0]["location"].startswith("研究 ·")
    assert chunks[1]["location"].startswith("Methods ·")
    assert all(chunk["page"] is None for chunk in chunks)
    long = parse_markdown(("# Heading\n" + "a" * (CHUNK_CHARS * 3)).encode())
    assert len(long) == 4
    assert all(len(chunk["text"]) <= CHUNK_CHARS for chunk in long)
    assert long[-1]["line_start"] == long[-1]["line_end"] == 2
    setext = parse_markdown(b"Title\n=====\nText\n```\n# not a heading\n```\n")
    assert len(setext) == 1 and setext[0]["location"].startswith("Title ·")
    assert parse_markdown(b"\xef\xbb\xbf# BOM")[0]["text"] == "# BOM"


@pytest.mark.parametrize("data", [b"\xff\xfe", b"\x80", b"", b" \n\t"])
def test_markdown_invalid_or_empty(data):
    with pytest.raises(DocumentError):
        parse_document("markdown", data)


def test_markdown_character_limit(monkeypatch):
    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 4)
    with pytest.raises(DocumentError, match="字符"):
        parse_document("markdown", "中文字数超限".encode())


def test_documents_never_execute_markdown(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Subprocess execution is forbidden")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    target = tmp_path / "executed"
    source = f"```python\nopen({str(target)!r}, 'w').write('bad')\n```\n<script>alert(1)</script>"
    status, chunks = parse_document("markdown", source.encode())
    assert status == "ready" and "<script>" in chunks[0]["text"]
    assert not target.exists()


def test_pdf_text_pages_scans_encryption_and_limits(monkeypatch):
    status, chunks = parse_document("pdf", pdf_bytes(pages=2))
    assert status == "ready"
    assert [chunk["page"] for chunk in chunks] == [1, 2]
    assert all("Research evidence" in chunk["text"] for chunk in chunks)
    assert all(chunk["line_start"] is None for chunk in chunks)
    assert parse_document("pdf", pdf_bytes(text=None)) == ("needs_ocr", [])
    with pytest.raises(DocumentError, match="加密"):
        parse_document("pdf", pdf_bytes(password="secret"))
    with pytest.raises(DocumentError):
        parse_document("pdf", b"%PDF-1.7 broken file")
    with pytest.raises(DocumentError, match="300"):
        parse_document("pdf", pdf_bytes(text=None, pages=301))
    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 4)
    with pytest.raises(DocumentError, match="字符"):
        parse_document("pdf", pdf_bytes())


def test_pdf_duplicate_media_box_keeps_text_and_limits(monkeypatch):
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    text = b"BT /F1 12 Tf 50 750 Td (Research evidence) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] "
        b"/MediaBox [0 0 612 792] /MediaBox [0 0 612 792] >>",
        b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    data += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
    data += str(xref).encode() + b"\n%%EOF\n"

    with pytest.raises(PdfReadError, match="Multiple definitions.*MediaBox"):
        len(PdfReader(BytesIO(data), strict=True).pages)
    status, chunks = parse_document("pdf", data)
    assert status == "ready"
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Research evidence"
    assert chunks[0]["page"] == 1
    assert chunks[0]["location"] == "第 1 页"
    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 4)
    with pytest.raises(DocumentError, match="字符"):
        parse_document("pdf", data)


def test_pdf_compressed_stream_expansion_is_bounded(monkeypatch):
    from pypdf import PdfWriter, get_configuration
    from pypdf.generic import DecodedStreamObject, NameObject

    original_limit = get_configuration().zlib_maximum_output_length
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = DecodedStreamObject()
    stream.set_data(b" " * 5000)
    page[NameObject("/Contents")] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    monkeypatch.setattr(documents, "MAX_PDF_STREAM_BYTES", 1024)
    with pytest.raises(DocumentError, match="安全限制"):
        parse_document("pdf", output.getvalue())
    assert get_configuration().zlib_maximum_output_length == original_limit


def test_dedup_is_project_local_and_blobs_are_generated(store, projects):
    first, second = projects
    data = "# 研究\n中文文献检索 research evidence".encode()
    original = store.create_document(first, "../../研究.md", data)
    duplicate = store.create_document(first, "different.md", data)
    foreign = store.create_document(second, "研究.md", data)
    assert original == duplicate
    assert original["id"] != foreign["id"]
    assert original["name"] == "研究.md"
    blobs = list(store.blob_dir.iterdir())
    assert len(blobs) == 2 and all(len(path.name) == 32 for path in blobs)
    _process_document(store, first, original["id"])
    _process_document(store, second, foreign["id"])
    assert store.search(first, "文献检索")[0]["document_id"] == original["id"]
    assert store.search(second, "research")[0]["document_id"] == foreign["id"]
    first_chunks = store.get_document_chunks(first, original["id"])
    second_chunks = store.get_document_chunks(second, foreign["id"])
    assert first_chunks[0]["id"] != second_chunks[0]["id"]
    assert set(first_chunks[0]) == {
        "id",
        "document_id",
        "name",
        "text",
        "location",
        "page",
        "line_start",
        "line_end",
    }
    assert store.read_document_file(first, original["id"])[1] == data
    store.delete_document(first, original["id"])
    assert store.search(first, "research") == []
    assert len(list(store.blob_dir.iterdir())) == 1
    assert store.search(second, "research")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM chunk_fts").fetchone()[0] == 1
    with pytest.raises(KeyError):
        store.get_document(first, original["id"])


def test_concurrent_dedup_has_one_blob(store, projects):
    def create(_):
        return store.create_document(projects[0], "x.md", b"Same research content")["id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        identifiers = list(pool.map(create, range(8)))
    assert len(set(identifiers)) == 1
    assert len(list(store.blob_dir.iterdir())) == 1


@pytest.mark.parametrize("filename", ["x.html", "x.txt", "x.docx", "../", "bad\x00.md"])
def test_store_rejects_unsupported_filenames(store, projects, filename):
    with pytest.raises(ValueError):
        store.create_document(projects[0], filename, b"data")
    assert list(store.blob_dir.iterdir()) == []


def test_store_rejects_large_upload_before_blob_creation(store, projects):
    with pytest.raises(ValueError, match="20 MiB"):
        store.create_document(projects[0], "x.md", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert store.list_documents(projects[0]) == []
    assert list(store.blob_dir.iterdir()) == []


def test_fts_search_tokens_sanitization_limits_and_selection(store, projects):
    first, second = projects
    assert search_tokens("Trial OR 临床研究") == ["trial", "or", "临床", "床研", "研究"]
    assert search_tokens("ＴＲＩＡＬ") == ["trial"]
    document = ingest(store, first, "# 研究\n临床研究证据\n" + " trial" * 3000)
    unrelated = ingest(store, first, "# Nothing\nSomething else", "other.md")
    foreign = ingest(store, second, "trial foreign content")
    assert len(store.search(first, "TRIAL", limit=100)) == 6
    assert store.search(first, "临床研究")[0]["document_id"] == document["id"]
    assert store.search(second, "临床研究") == []
    assert store.search(first, "trial", [unrelated["id"]]) == []
    assert store.search(first, "trial", []) == []
    assert store.search(first, '" * () : -') == []
    assert store.search(first, "") == []
    assert store.search(first, 'trial" OR project_id:* --')
    assert store.search(first, "' ; DROP TABLE documents; --") == []
    for chunk in store.search(first, "trial"):
        assert len(chunk["text"]) <= 452 and chunk["location"]
        assert chunk["document_id"] != foreign["id"]
    assert len(store.list_documents(first)) == 2
    with pytest.raises(ValueError):
        store.search(first, "x" * 2001)
    with pytest.raises(ValueError):
        store.search(first, "trial", limit=0)
    with pytest.raises(KeyError):
        store.search(first, "trial", [document["id"], foreign["id"]])


def test_restart_marks_interrupted_jobs_and_retry_can_recover(store, projects):
    project = projects[0]
    pending = store.create_document(project, "pending.md", b"pending content")
    processing = store.create_document(project, "processing.md", b"processing content")
    old_generation, _, _ = store.claim_document(project, processing["id"])
    ready = ingest(store, project, "ready content", "ready.md")
    reopened = WorkspaceStore(store.path, store.blob_dir)
    for document in (pending, processing):
        current = reopened.get_document(project, document["id"])
        assert current["status"] == "failed" and current["error"] == "服务重启，请重试"
    assert reopened.get_document(project, ready["id"])["status"] == "ready"
    assert not store.publish_document(
        project, processing["id"], old_generation, "ready", parse_markdown(b"stale")
    )
    assert reopened.retry_document(project, processing["id"])["status"] == "pending"
    asyncio.run(documents.process_document(reopened, project, processing["id"]))
    assert reopened.get_document(project, processing["id"])["status"] == "ready"
    assert reopened.search(project, "processing")


def test_generations_prevent_stale_publish_and_failure(store, projects):
    project = projects[0]
    document = store.create_document(project, "x.md", b"fresh")
    identifier = document["id"]
    old_generation, _, _ = store.claim_document(project, identifier)
    assert store.claim_document(project, identifier) is None
    store.retry_document(project, identifier)
    new_generation, _, _ = store.claim_document(project, identifier)
    assert new_generation > old_generation
    assert not store.publish_document(
        project, identifier, old_generation, "ready", parse_markdown(b"stale")
    )
    assert not store.fail_document(project, identifier, old_generation, "stale error")
    assert store.publish_document(
        project, identifier, new_generation, "ready", parse_markdown(b"fresh")
    )
    assert not store.publish_document(
        project, identifier, new_generation, "ready", parse_markdown(b"duplicate")
    )
    assert store.get_document(project, identifier)["chunk_count"] == 1
    assert store.search(project, "stale") == []
    assert store.search(project, "fresh")
    store.retry_document(project, identifier)
    assert store.get_document_chunks(project, identifier) == []
    assert store.search(project, "fresh") == []
    generation, _, _ = store.claim_document(project, identifier)
    store.delete_document(project, identifier)
    assert not store.publish_document(
        project, identifier, generation, "ready", parse_markdown(b"old")
    )
    assert not store.fail_document(project, identifier, generation, "old")
    assert store.claim_document(project, identifier) is None
    assert list(store.blob_dir.iterdir()) == []


def test_delete_during_parsing_cannot_resurrect_document(store, projects, monkeypatch):
    project = projects[0]
    identifier = store.create_document(project, "race.md", b"evidence")["id"]
    entered, release = Event(), Event()
    original = documents.parse_document

    def blocked_parse(kind, data):
        entered.set()
        assert release.wait(timeout=5)
        return original(kind, data)

    monkeypatch.setattr(documents, "parse_document", blocked_parse)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(_process_document, store, project, identifier)
        try:
            assert entered.wait(timeout=5)
            store.delete_document(project, identifier)
        finally:
            release.set()
        job.result(timeout=5)
    assert store.list_documents(project) == []
    assert store.search(project, "evidence") == []
    assert list(store.blob_dir.iterdir()) == []


def test_failed_publish_rolls_back_chunks_and_index(store, projects):
    project = projects[0]
    identifier = store.create_document(project, "x.md", b"data")["id"]
    generation, _, _ = store.claim_document(project, identifier)
    chunks = parse_markdown(b"evidence") + [{"text": "x" * (CHUNK_CHARS + 1), "location": "bad"}]
    with pytest.raises(ValueError):
        store.publish_document(project, identifier, generation, "ready", chunks)
    assert store.get_document_chunks(project, identifier) == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM chunk_fts").fetchone()[0] == 0
    assert store.fail_document(project, identifier, generation, "failure")
    assert store.get_document(project, identifier)["status"] == "failed"


def test_worker_failure_and_ocr_states(store, projects):
    project = projects[0]
    for filename, data, expected in (
        ("invalid.md", b"\xff", "failed"),
        ("invalid.pdf", b"broken", "failed"),
        ("scan.pdf", pdf_bytes(text=None), "needs_ocr"),
        ("encrypted.pdf", pdf_bytes(password="secret"), "failed"),
    ):
        document = store.create_document(project, filename, data)
        _process_document(store, project, document["id"])
        current = store.get_document(project, document["id"])
        assert current["status"] == expected and current["chunk_count"] == 0
        assert bool(current["error"]) == (expected == "failed")
    missing = store.create_document(project, "missing.md", b"missing file")
    _, _, path = store.claim_document(project, missing["id"])
    path.unlink()
    store.retry_document(project, missing["id"])
    _process_document(store, project, missing["id"])
    assert store.get_document(project, missing["id"])["status"] == "failed"


def test_http_project_session_outline_contract(client, store):
    assert client.get("/api/projects").json() == []
    response = client.post("/api/projects", json={"name": "  Research  "})
    assert response.status_code == 201
    project = response.json()
    assert set(project) == {"id", "name", "created_at"} and project["name"] == "Research"
    base = f"/api/projects/{project['id']}"
    assert client.patch(base, json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert client.get(base + "/sessions").json() == []
    response = client.post(base + "/sessions", json={"title": "Session"})
    assert response.status_code == 201
    session = response.json()
    session_url = base + f"/sessions/{session['id']}"
    assert client.get(session_url).json() == {"session": session, "messages": []}
    message = {"id": "m1", "role": "assistant", "parts": [{"type": "text", "text": "Reply"}]}
    store.save_message(project["id"], session["id"], message)
    assert client.get(session_url).json()["messages"] == [message]
    assert client.patch(session_url, json={"title": "New title"}).json()["title"] == "New title"
    assert client.put(session_url + "/messages", json=[message]).status_code in {404, 405}
    assert client.get(base + "/outline").json() == {"sections": [], "version": 0}
    body = {"sections": [{"id": "intro", "title": " Introduction "}], "version": 0}
    saved = client.put(base + "/outline", json=body)
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert saved.json()["sections"][0]["title"] == "Introduction"
    assert client.put(base + "/outline", json=body).status_code == 409
    assert client.put(base + "/outline", json={"sections": [], "version": 1}).json()["version"] == 2
    assert client.post("/api/projects", json={"name": " "}).status_code == 422
    assert client.post(base + "/sessions", json={"title": "x" * 201}).status_code == 422
    duplicate = [{"id": "x", "title": "One"}, {"id": "x", "title": "Two"}]
    assert (
        client.put(base + "/outline", json={"sections": duplicate, "version": 2}).status_code == 422
    )
    assert client.put(base + "/outline", json={"sections": [], "version": True}).status_code == 422


def test_http_upload_download_search_retry_delete(client, store, projects):
    project = projects[0]
    base = f"/api/projects/{project}"
    data = "# 研究\n临床研究 evidence\n<script>alert(1)</script>".encode()
    response = client.post(
        base + "/documents",
        params={"filename": "../../研究.md"},
        content=data,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "pending" and document["error"] is None
    assert set(document) == {
        "id",
        "project_id",
        "name",
        "kind",
        "status",
        "error",
        "chunk_count",
        "created_at",
    }
    url = base + f"/documents/{document['id']}"
    detail = client.get(url).json()
    assert detail["document"]["status"] == "ready"
    assert detail["document"]["chunk_count"] == len(detail["chunks"]) == 1
    assert client.get(base + "/documents").json() == [detail["document"]]
    results = client.get(base + "/search", params={"q": "临床研究"}).json()
    assert results[0]["document_id"] == document["id"] and results[0]["location"]
    assert client.get(base + "/search", params={"q": "x" * 2001}).status_code == 422
    download = client.get(url + "/file")
    assert download.content == data
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-type"].startswith("text/markdown")
    duplicate = client.post(
        base + "/documents",
        params={"filename": "another.md"},
        content=data,
        headers={"content-type": "application/octet-stream"},
    )
    assert duplicate.json()["id"] == document["id"]
    assert client.post(url + "/retry").json()["status"] == "pending"
    assert client.get(url).json()["document"]["status"] == "ready"
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    assert client.get(url + "/file").status_code == 404
    assert client.get(base + "/search", params={"q": "evidence"}).json() == []
    assert list(store.blob_dir.iterdir()) == []


def test_http_pdf_ingest_and_safe_download(client, projects):
    base = f"/api/projects/{projects[0]}/documents"
    data = pdf_bytes()
    response = client.post(
        base,
        params={"filename": "paper.PDF"},
        content=data,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 201 and response.json()["kind"] == "pdf"
    url = base + "/" + response.json()["id"]
    detail = client.get(url).json()
    assert detail["document"]["status"] == "ready" and detail["chunks"][0]["page"] == 1
    download = client.get(url + "/file")
    assert download.content == data and download.headers["content-type"] == "application/pdf"
    assert download.headers["x-content-type-options"] == "nosniff"


def test_http_cross_project_access_is_404(client, store, projects):
    first, second = projects
    document = ingest(store, first, "evidence")["id"]
    session = store.create_session(first, "Session")["id"]
    for project in (second, "missing"):
        base = f"/api/projects/{project}"
        for method, path, kwargs in (
            ("get", f"/documents/{document}", {}),
            ("get", f"/documents/{document}/file", {}),
            ("post", f"/documents/{document}/retry", {}),
            ("delete", f"/documents/{document}", {}),
            ("get", "/search", {"params": {"q": "evidence", "document_id": document}}),
            ("get", "/search", {"params": {"q": "", "document_id": document}}),
            ("get", f"/sessions/{session}", {}),
            ("patch", f"/sessions/{session}", {"json": {"title": "Changed"}}),
        ):
            response = getattr(client, method)(base + path, **kwargs)
            assert response.status_code == 404, (method, path, response.text)
    assert client.get(f"/api/projects/{second}/search", params={"q": "evidence"}).json() == []
    assert store.get_document(first, document)["status"] == "ready"


def test_http_missing_project_collections_are_404(client):
    base = "/api/projects/missing"
    for suffix in ("/sessions", "/documents", "/outline", "/search"):
        assert client.get(base + suffix).status_code == 404
    assert client.patch(base, json={"name": "Name"}).status_code == 404
    assert client.post(base + "/sessions", json={"title": "Name"}).status_code == 404
    assert client.put(base + "/outline", json={"sections": [], "version": 0}).status_code == 404
    assert (
        client.post(
            base + "/documents",
            params={"filename": "x.md"},
            content=b"data",
            headers={"content-type": "application/octet-stream"},
        ).status_code
        == 404
    )


def test_http_upload_type_and_declared_size_validation(client, store, projects):
    url = f"/api/projects/{projects[0]}/documents"
    assert client.post(url, params={"filename": "x.md"}, content=b"text").status_code == 415
    headers = {"content-type": "application/octet-stream"}
    assert (
        client.post(
            url, params={"filename": "x.html"}, content=b"text", headers=headers
        ).status_code
        == 415
    )
    assert (
        client.post(
            url,
            params={"filename": "x.md"},
            content=b"text",
            headers={**headers, "content-length": str(MAX_UPLOAD_BYTES + 1)},
        ).status_code
        == 413
    )
    for length in ("-1", "invalid"):
        assert (
            client.post(
                url,
                params={"filename": "x.md"},
                content=b"text",
                headers={**headers, "content-length": length},
            ).status_code
            == 400
        )
    assert store.list_documents(projects[0]) == []


@pytest.mark.parametrize("declared", [None, b"1"])
async def test_upload_enforces_stream_limit_without_trusting_length(store, projects, declared):
    headers = [(b"content-type", b"application/octet-stream")]
    if declared is not None:
        headers.append((b"content-length", declared))
    received = 0

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * (1024 * 1024), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)
    with pytest.raises(HTTPException) as error:
        await workspace.upload_document(projects[0], request, BackgroundTasks(), store, "x.md")
    assert error.value.status_code == 413 and received == 21
    assert store.list_documents(projects[0]) == []
    assert list(store.blob_dir.iterdir()) == []


def test_unconfigured_workspace_is_503():
    app = FastAPI()
    app.include_router(workspace.router)
    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 503


def test_sqlite_foreign_keys_and_message_serialization_rollback(store, projects):
    session = store.create_session(projects[0], "Session")["id"]
    with store._connection(write=True) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO messages(session_id, id, body) VALUES (?, ?, ?)",
                ("missing", "m1", "{}"),
            )
    with pytest.raises(ValueError):
        store.save_message(projects[0], session, {"id": "m1", "value": float("nan")})
    assert store.session_messages(projects[0], session) == []
    message = {"id": "m1", "parts": [{"type": "data-citation", "data": {"text": "中文"}}]}
    store.save_message(projects[0], session, message)
    assert json.loads(json.dumps(store.session_messages(projects[0], session)))[0] == message
