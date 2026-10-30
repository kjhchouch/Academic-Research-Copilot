from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from api.documents import MAX_UPLOAD_BYTES, DocumentError, document_name, process_document
from api.workspace_store import WorkspaceConflict, WorkspaceStore

router = APIRouter(prefix="/api/projects", tags=["workspace"])
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class NameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Title


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title


class OutlineSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    title: Title


class OutlineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[OutlineSection] = Field(max_length=100)
    version: StrictInt = Field(ge=0)


class NoteCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    session_id: str | None = None
    message_id: str | None = None


class NoteUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Title
    body: str = Field(max_length=100000)
    version: StrictInt = Field(ge=0)


def get_store(request: Request) -> WorkspaceStore:
    store = getattr(request.app.state, "workspace", None)
    if store is None:
        raise HTTPException(status_code=503, detail="工作区尚未初始化")
    return store


Store = Annotated[WorkspaceStore, Depends(get_store)]


async def _call(function: Callable[..., Any], *args: Any) -> Any:
    try:
        return await asyncio.to_thread(function, *args)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="项目或资源不存在") from exc
    except WorkspaceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_projects(store: Store) -> list[dict]:
    return await _call(store.list_projects)


@router.post("", status_code=201)
async def create_project(body: NameInput, store: Store) -> dict:
    return await _call(store.create_project, body.name)


@router.patch("/{project_id}")
async def rename_project(project_id: str, body: NameInput, store: Store) -> dict:
    return await _call(store.rename_project, project_id, body.name)


@router.get("/{project_id}/sessions")
async def list_sessions(project_id: str, store: Store) -> list[dict]:
    return await _call(store.list_sessions, project_id)


@router.post("/{project_id}/sessions", status_code=201)
async def create_session(project_id: str, body: SessionInput, store: Store) -> dict:
    return await _call(store.create_session, project_id, body.title)


@router.get("/{project_id}/sessions/{session_id}")
async def get_session(project_id: str, session_id: str, store: Store) -> dict:
    session = await _call(store.get_session, project_id, session_id)
    messages = await _call(store.session_messages, project_id, session_id)
    return {"session": session, "messages": messages}


@router.patch("/{project_id}/sessions/{session_id}")
async def rename_session(
    project_id: str, session_id: str, body: SessionInput, store: Store
) -> dict:
    return await _call(store.rename_session, project_id, session_id, body.title)


@router.get("/{project_id}/outline")
async def get_outline(project_id: str, store: Store) -> dict:
    return await _call(store.get_outline, project_id)


@router.put("/{project_id}/outline")
async def save_outline(project_id: str, body: OutlineInput, store: Store) -> dict:
    return await _call(
        store.save_outline,
        project_id,
        [section.model_dump() for section in body.sections],
        body.version,
    )


@router.get("/{project_id}/notes")
async def list_notes(project_id: str, store: Store) -> list[dict]:
    return await _call(store.list_notes, project_id)


@router.post("/{project_id}/notes", status_code=201)
async def create_note(project_id: str, body: NoteCreateInput, store: Store) -> dict:
    return await _call(store.create_note, project_id, body.title, body.session_id, body.message_id)


@router.get("/{project_id}/notes/{note_id}")
async def get_note(project_id: str, note_id: str, store: Store) -> dict:
    return await _call(store.get_note, project_id, note_id)


@router.put("/{project_id}/notes/{note_id}")
async def save_note(project_id: str, note_id: str, body: NoteUpdateInput, store: Store) -> dict:
    return await _call(store.save_note, project_id, note_id, body.title, body.body, body.version)


@router.delete("/{project_id}/notes/{note_id}", status_code=204)
async def delete_note(project_id: str, note_id: str, store: Store) -> Response:
    await _call(store.delete_note, project_id, note_id)
    return Response(status_code=204)


@router.get("/{project_id}/documents")
async def list_documents(project_id: str, store: Store) -> list[dict]:
    return await _call(store.list_documents, project_id)


@router.post("/{project_id}/documents", status_code=201)
async def upload_document(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    store: Store,
    filename: str = Query(min_length=1, max_length=1024),
) -> dict:
    await _call(store.get_project, project_id)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/octet-stream"
    ):
        raise HTTPException(status_code=415, detail="请使用 application/octet-stream 上传原始文件")
    try:
        document_name(filename)
    except DocumentError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
        if length < 0:
            raise HTTPException(status_code=400, detail="Content-Length 无效")
        if length > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 20 MiB 限制")
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过 20 MiB 限制")
        data.extend(part)
    document = await _call(store.create_document, project_id, filename, bytes(data))
    if document["status"] == "pending":
        background_tasks.add_task(process_document, store, project_id, document["id"])
    return document


@router.get("/{project_id}/documents/{document_id}")
async def get_document(project_id: str, document_id: str, store: Store) -> dict:
    document = await _call(store.get_document, project_id, document_id)
    chunks = await _call(store.get_document_chunks, project_id, document_id)
    return {"document": document, "chunks": chunks}


@router.get("/{project_id}/documents/{document_id}/file")
async def download_document(project_id: str, document_id: str, store: Store) -> Response:
    document, data = await _call(store.read_document_file, project_id, document_id)
    pdf = document["kind"] == "pdf"
    suffix = "pdf" if pdf else "md"
    return Response(
        content=data,
        media_type="application/pdf" if pdf else "text/markdown",
        headers={
            "Content-Disposition": (
                f'attachment; filename="document.{suffix}"; '
                f"filename*=UTF-8''{quote(document['name'], safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.post("/{project_id}/documents/{document_id}/retry")
async def retry_document(
    project_id: str,
    document_id: str,
    background_tasks: BackgroundTasks,
    store: Store,
) -> dict:
    document = await _call(store.retry_document, project_id, document_id)
    background_tasks.add_task(process_document, store, project_id, document_id)
    return document


@router.delete("/{project_id}/documents/{document_id}", status_code=204)
async def delete_document(project_id: str, document_id: str, store: Store) -> Response:
    await _call(store.delete_document, project_id, document_id)
    return Response(status_code=204)


@router.get("/{project_id}/search")
async def search(
    project_id: str,
    store: Store,
    q: str = Query(default="", max_length=2000),
    document_id: str | None = None,
) -> list[dict]:
    return await _call(
        store.search, project_id, q, [document_id] if document_id is not None else None
    )
