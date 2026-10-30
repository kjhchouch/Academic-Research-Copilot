from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from api.documents import _process_document
from api.routes import workspace
from api.workspace_store import WorkspaceConflict, WorkspaceStore
from fastapi import FastAPI
from fastapi.testclient import TestClient


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


@pytest.fixture
def origin(store, projects):
    project = projects[0]
    session = store.create_session(project, "Research")["id"]
    document = store.create_document(project, "paper.md", b"Research evidence")
    _process_document(store, project, document["id"])
    chunk = store.get_document_chunks(project, document["id"])[0]
    source = {
        "source_id": "K1",
        "project_id": project,
        "document_id": document["id"],
        "chunk_id": chunk["id"],
        "name": "paper.md",
        "text": "Research evidence",
        "location": chunk["location"],
    }
    citation = {"citation_id": "1", "title": "Published study", "doi": "10.1234/example"}
    message = {
        "id": "answer",
        "role": "assistant",
        "parts": [
            {"type": "text", "text": "Evidence [K1].\n"},
            {"type": "data-knowledge-source", "data": source},
            {"type": "dynamic-tool", "output": {"private": "not note content"}},
            {"type": "data-citation", "data": citation},
            {"type": "text", "text": "结论 [1]."},
            {"type": "data-turn-status", "data": {"status": "complete"}},
        ],
    }
    store.save_message(project, session, message)
    return session, message, source, citation


def adopt(client, project, origin, title="  Research outcome  "):
    return client.post(
        f"/api/projects/{project}/notes",
        json={"title": title, "session_id": origin[0], "message_id": origin[1]["id"]},
    )


def test_blank_note_crud_persistence_and_order(client, store, projects):
    project = projects[0]
    base = f"/api/projects/{project}/notes"
    assert client.get(base).json() == []
    response = client.post(base, json={"title": "  新笔记  "})
    assert response.status_code == 201
    note = response.json()
    assert set(note) == {
        "id",
        "project_id",
        "title",
        "body",
        "sources",
        "citations",
        "origin_session_id",
        "origin_message_id",
        "version",
        "created_at",
        "updated_at",
    }
    assert note == {
        **note,
        "project_id": project,
        "title": "新笔记",
        "body": "",
        "sources": [],
        "citations": [],
        "origin_session_id": None,
        "origin_message_id": None,
        "version": 0,
    }
    assert datetime.fromisoformat(note["created_at"]).tzinfo is not None
    assert note["created_at"] == note["updated_at"]
    url = base + "/" + note["id"]
    assert client.get(url).json() == note
    second = client.post(base, json={"title": "Second blank"}).json()
    assert second["id"] != note["id"]
    assert client.get(base).json() == [second, note]
    payload = {"title": "  Edited  ", "body": "  Markdown\n\n中文  ", "version": 0}
    response = client.put(url, json=payload)
    assert response.status_code == 200
    edited = response.json()
    assert edited == {
        **note,
        "title": "Edited",
        "body": payload["body"],
        "version": 1,
        "updated_at": edited["updated_at"],
    }
    assert edited["updated_at"] > note["updated_at"]
    assert client.get(base).json() == [edited, second]
    reopened = WorkspaceStore(store.path, store.blob_dir)
    client.app.state.workspace = reopened
    assert client.get(url).json() == edited
    assert reopened.list_notes(project) == [edited, second]
    assert client.put(url, json=payload).status_code == 409
    assert client.get(url).json() == edited
    deleted = client.delete(url)
    assert deleted.status_code == 204 and deleted.content == b""
    assert client.get(url).status_code == 404
    assert client.delete(url).status_code == 404
    assert WorkspaceStore(store.path, store.blob_dir).list_notes(project) == [second]


def test_adoption_uses_exact_persisted_message_and_is_idempotent(client, store, projects, origin):
    project = projects[0]
    session, message, source, citation = origin
    store.save_message(
        project,
        session,
        {
            "id": "later",
            "role": "assistant",
            "parts": [
                {"type": "text", "text": "Do not adopt the latest message"},
                {"type": "data-citation", "data": {"title": "Wrong citation"}},
                {"type": "data-turn-status", "data": {"status": "complete"}},
            ],
        },
    )
    response = adopt(client, project, origin)
    assert response.status_code == 201
    note = response.json()
    assert note["title"] == "Research outcome"
    assert note["body"] == "Evidence [K1].\n结论 [1]."
    assert note["sources"] == [source] and note["citations"] == [citation]
    assert note["origin_session_id"] == session
    assert note["origin_message_id"] == message["id"]
    url = f"/api/projects/{project}/notes/{note['id']}"
    edited = client.put(url, json={"title": "My edits", "body": "Kept", "version": 0}).json()
    assert adopt(client, project, origin, "Do not overwrite").json() == edited
    assert client.get(f"/api/projects/{project}/notes").json() == [edited]
    message["parts"][0]["text"] = "Changed original"
    source["text"] = "Changed source"
    citation["title"] = "Changed citation"
    store.save_message(project, session, message)
    assert client.get(url).json() == edited
    assert adopt(client, project, origin).json() == edited
    assert WorkspaceStore(store.path, store.blob_dir).get_note(project, note["id"]) == edited


@pytest.mark.parametrize("delete_before_adoption", [False, True])
def test_deleted_document_preserves_snapshots(
    client,
    store,
    projects,
    origin,
    delete_before_adoption,
):
    project = projects[0]
    if delete_before_adoption:
        store.delete_document(project, origin[2]["document_id"])
    response = adopt(client, project, origin)
    assert response.status_code == 201
    note = response.json()
    if not delete_before_adoption:
        store.delete_document(project, origin[2]["document_id"])
    assert store.list_documents(project) == []
    client.app.state.workspace = WorkspaceStore(store.path, store.blob_dir)
    url = f"/api/projects/{project}/notes/{note['id']}"
    assert client.get(url).json() == note
    updated = client.put(url, json={"title": "Edited", "body": "", "version": 0})
    assert updated.status_code == 200
    assert updated.json()["sources"] == [origin[2]]
    assert updated.json()["citations"] == [origin[3]]
    assert adopt(client, project, origin).json() == updated.json()


def test_all_endpoints_and_origins_are_scoped(client, store, projects, origin):
    first, second = projects
    note = adopt(client, first, origin).json()
    for scope in (second, "missing"):
        base = f"/api/projects/{scope}/notes"
        for method, kwargs in (
            ("get", {}),
            ("put", {"json": {"title": "Other", "body": "bad", "version": 0}}),
            ("delete", {}),
        ):
            response = getattr(client, method)(base + "/" + note["id"], **kwargs)
            assert response.status_code == 404
            assert response.json() == {"detail": "项目或资源不存在"}
        assert adopt(client, scope, origin).status_code == 404
    assert client.get(f"/api/projects/{second}/notes").json() == []
    assert client.get("/api/projects/missing/notes").status_code == 404
    assert client.post("/api/projects/missing/notes", json={"title": "Blank"}).status_code == 404
    local_session = store.create_session(first, "Empty local session")["id"]
    foreign_session = store.create_session(second, "Foreign session")["id"]
    foreign_message = {**origin[1], "id": "foreign-only"}
    store.save_message(second, foreign_session, foreign_message)
    for session, message in (
        (foreign_session, "foreign-only"),
        (origin[0], "foreign-only"),
        (local_session, origin[1]["id"]),
        (origin[0], "missing"),
        ("missing", origin[1]["id"]),
    ):
        response = client.post(
            f"/api/projects/{first}/notes",
            json={
                "title": "Wrong origin",
                "session_id": session,
                "message_id": message,
            },
        )
        assert response.status_code == 404
    foreign_blank = client.post(f"/api/projects/{second}/notes", json={"title": "Blank"})
    assert foreign_blank.status_code == 201
    assert foreign_blank.json()["project_id"] == second
    assert client.get(f"/api/projects/{first}/notes").json() == [note]


@pytest.mark.parametrize(
    "role,status,text",
    [
        ("user", "complete", "User text"),
        ("system", "complete", "System text"),
        ("assistant", None, "Partial"),
        ("assistant", "partial", "Partial"),
        ("assistant", "interrupted", "Interrupted"),
        ("assistant", "error", "Failed"),
        ("assistant", "streaming", "Streaming"),
        ("assistant", "complete", None),
        ("assistant", "complete", ""),
        ("assistant", "complete", " \n\t "),
        ("assistant", "complete", 42),
    ],
)
def test_rejects_non_completed_assistant_text(client, store, projects, role, status, text):
    project = projects[0]
    session = store.create_session(project, "Session")["id"]
    parts = [] if text is None else [{"type": "text", "text": text}]
    if status is not None:
        parts.append({"type": "data-turn-status", "data": {"status": status}})
    message = {"id": "m", "role": role, "parts": parts}
    store.save_message(project, session, message)
    response = adopt(client, project, (session, message))
    assert response.status_code == 422
    assert client.get(f"/api/projects/{project}/notes").json() == []


@pytest.mark.parametrize(
    "part",
    [
        None,
        {"type": "data-turn-status", "data": None},
        {"type": "data-turn-status", "data": {"status": "error"}},
        {"type": "data-knowledge-source", "data": []},
        {"type": "data-citation", "data": "not a snapshot"},
    ],
)
def test_malformed_parts_are_rejected_without_internal_errors(
    client, store, projects, origin, part
):
    origin[1]["parts"].append(part)
    store.save_message(projects[0], origin[0], origin[1])
    response = adopt(client, projects[0], origin)
    assert response.status_code == 422
    assert "Traceback" not in response.text and str(store.path) not in response.text


@pytest.mark.parametrize("scope", ["foreign", "missing", None])
def test_knowledge_source_scope_is_required(client, store, projects, origin, scope):
    if scope == "missing":
        del origin[2]["project_id"]
    else:
        origin[2]["project_id"] = projects[1] if scope == "foreign" else None
    store.save_message(projects[0], origin[0], origin[1])
    assert adopt(client, projects[0], origin).status_code == 422
    assert client.get(f"/api/projects/{projects[0]}/notes").json() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"body": "Client answer"},
        {"sources": [{"project_id": "fake"}]},
        {"citations": [{"doi": "fake"}]},
        {"version": 0},
        {"origin_session_id": "fake"},
        {"origin_message_id": "fake"},
        {"message": {"role": "assistant"}},
        {"project_id": "fake"},
    ],
)
def test_create_rejects_client_content_and_unknown_fields(client, projects, origin, extra):
    base = f"/api/projects/{projects[0]}/notes"
    for payload in (
        {"title": "Blank"},
        {"title": "Adopt", "session_id": origin[0], "message_id": origin[1]["id"]},
    ):
        assert client.post(base, json={**payload, **extra}).status_code == 422
    assert client.get(base).json() == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": ""},
        {"title": " \n "},
        {"title": "x" * 201},
        {"title": None},
        {"title": 12},
        {"title": "T", "session_id": "s"},
        {"title": "T", "message_id": "m"},
        {"title": "T", "session_id": "", "message_id": ""},
        {"title": "T", "session_id": 1, "message_id": "m"},
        {"title": "T", "session_id": "s", "message_id": None},
    ],
)
def test_create_validation(client, projects, payload):
    assert client.post(f"/api/projects/{projects[0]}/notes", json=payload).status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": " "},
        {"title": "x" * 201},
        {"title": None},
        {"body": "x" * 100001},
        {"body": None},
        {"body": 42},
        {"body": []},
        {"version": -1},
        {"version": True},
        {"version": "0"},
        {"version": 0.0},
        {"version": None},
        {"sources": []},
        {"citations": []},
        {"origin_session_id": None},
        {"origin_message_id": None},
        {"project_id": "fake"},
    ],
)
def test_update_validation_and_immutable_fields(client, projects, changes):
    base = f"/api/projects/{projects[0]}/notes"
    note = client.post(base, json={"title": "Original"}).json()
    url = base + "/" + note["id"]
    response = client.put(url, json={"title": "Edited", "body": "Text", "version": 0, **changes})
    assert response.status_code == 422
    assert client.get(url).json() == note


@pytest.mark.parametrize("missing", ["title", "body", "version"])
def test_update_requires_all_fields(client, projects, missing):
    base = f"/api/projects/{projects[0]}/notes"
    note = client.post(base, json={"title": "Original"}).json()
    payload = {"title": "Edited", "body": "Text", "version": 0}
    del payload[missing]
    assert client.put(base + "/" + note["id"], json=payload).status_code == 422


def test_title_and_body_limits_apply_to_creation_and_update(client, store, projects, origin):
    project = projects[0]
    base = f"/api/projects/{project}/notes"
    for length, expected in ((100001, 422), (100000, 201)):
        origin[1]["parts"] = [
            {"type": "text", "text": "中" * 50000},
            {"type": "text", "text": "文" * (length - 50000)},
            {"type": "data-turn-status", "data": {"status": "complete"}},
        ]
        store.save_message(project, origin[0], origin[1])
        response = adopt(client, project, origin, "  " + "题" * 200 + "  ")
        assert response.status_code == expected
    note = response.json()
    assert len(note["body"]) == 100000 and note["title"] == "题" * 200
    assert note["sources"] == [] and note["citations"] == []
    updated = client.put(
        base + "/" + note["id"],
        json={
            "title": " " + "T" * 200 + " ",
            "body": "x" * 100000,
            "version": 0,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "T" * 200 and len(updated.json()["body"]) == 100000


def test_concurrent_adoption_and_optimistic_writes(store, projects, origin):
    project = projects[0]

    def create(_):
        return store.create_note(project, "Adopt", origin[0], origin[1]["id"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        notes = list(pool.map(create, range(8)))
    assert all(note == notes[0] for note in notes)
    note = notes[0]

    def save(title):
        try:
            store.save_note(project, note["id"], title, "Body", 0)
            return "saved"
        except WorkspaceConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, ["First", "Second"])) == ["conflict", "saved"]
    assert store.get_note(project, note["id"])["version"] == 1
    with sqlite3.connect(store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO notes SELECT 'duplicate', project_id, title, body, sources, citations, "
            "origin_session_id, origin_message_id, version, created_at, updated_at FROM notes"
        )


def test_migration_preserves_existing_workspace_data(store, projects):
    project = projects[0]
    session = store.create_session(project, "Session")["id"]
    message = {"id": "m", "role": "user", "parts": [{"type": "text", "text": "Original"}]}
    store.save_message(project, session, message)
    outline = store.save_outline(project, [{"id": "intro", "title": "Intro"}], 0)
    document = store.create_document(project, "paper.md", b"Research evidence")
    _process_document(store, project, document["id"])
    chunks = store.get_document_chunks(project, document["id"])
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE IF EXISTS notes")
    reopened = WorkspaceStore(store.path, store.blob_dir)
    assert reopened.list_notes(project) == []
    note = reopened.create_note(project, "Blank")
    assert WorkspaceStore(store.path, store.blob_dir).get_note(project, note["id"]) == note
    assert reopened.list_projects() == store.list_projects()
    assert reopened.session_messages(project, session) == [message]
    assert reopened.get_outline(project) == outline
    assert reopened.get_document_chunks(project, document["id"]) == chunks
    assert reopened.read_document_file(project, document["id"])[1] == b"Research evidence"
    assert reopened.search(project, "Research")[0]["document_id"] == document["id"]
