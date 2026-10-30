from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from api.workspace_store import WorkspaceStore

# Put structural terms first: search keeps only the first 128 distinct tokens.
# A long user query must not crowd out methods/results in either language.
_RESEARCH_TERMS = (
    "研究问题 方法 样本量 结果 结论 局限 摘要 "
    "abstract research question methods sample size results conclusions limitations"
)
_MAX_QUERY_CHARS = 2000
_SOURCES_PER_DOCUMENT = 3


def build_comparison_context(
    store: WorkspaceStore, project_id: str, query: str, document_ids: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve bounded evidence per selected document; call via asyncio.to_thread.

    This is selective retrieval, not an exhaustive review of any document. Missing
    hits (including chunks removed during retrieval) never become invented sources.
    """
    if not 2 <= len(document_ids) <= 5 or len(set(document_ids)) != len(document_ids):
        raise ValueError("资料对比需要选择 2 至 5 份不重复的资料")
    documents = [store.get_document(project_id, identifier) for identifier in document_ids]
    if any(document["status"] != "ready" for document in documents):
        raise ValueError("资料对比仅支持解析就绪的资料")

    search_query = f"{_RESEARCH_TERMS} {query[: _MAX_QUERY_CHARS - len(_RESEARCH_TERMS) - 1]}"
    sources: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {"task": "compare", "documents": []}
    for document in documents:
        identifier = document["id"]
        hits = store.search(project_id, search_query, [identifier], limit=_SOURCES_PER_DOCUMENT)
        chunks = (
            {chunk["id"]: chunk for chunk in store.get_document_chunks(project_id, identifier)}
            if hits
            else {}
        )
        source_ids = []
        seen = set()
        for hit in hits[:_SOURCES_PER_DOCUMENT]:
            chunk = chunks.get(hit["id"])
            if (
                chunk is None
                or chunk["document_id"] != identifier
                or not chunk["text"].strip()
                or chunk["id"] in seen
            ):
                continue
            seen.add(chunk["id"])
            source_id = f"K{len(sources) + 1}"
            # Search text is a UI snippet. Only the stored full chunk is evidence.
            sources.append({**chunk, "project_id": project_id, "source_id": source_id})
            source_ids.append(source_id)
        coverage["documents"].append(
            {
                "document_id": identifier,
                "name": document["name"],
                "source_ids": source_ids,
                "status": "retrieved" if source_ids else "missing",
            }
        )
    return sources, coverage
