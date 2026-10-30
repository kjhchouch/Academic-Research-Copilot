from collections import Counter

import pytest
from api.documents import CHUNK_CHARS, search_tokens
from api.research import build_comparison_context
from api.workspace_store import WorkspaceStore


@pytest.fixture
def workspace(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite", tmp_path / "blobs")
    return store, store.create_project("Research")["id"]


def publish(store, project, name, texts):
    document = store.create_document(project, name, (name + "\n" + "\n".join(texts)).encode())
    identifier = document["id"]
    generation, _, _ = store.claim_document(project, identifier)
    store.publish_document(
        project,
        identifier,
        generation,
        "ready",
        [
            {
                "text": text,
                "location": f"第 {index} 页",
                "page": index,
                "line_start": None,
                "line_end": None,
            }
            for index, text in enumerate(texts, 1)
        ],
    )
    return identifier


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_comparison_retrieves_each_document_in_selection_order(workspace, count):
    store, project = workspace
    dominant = publish(store, project, "dominant.md", ["quantum focus"] * 8)
    others = [
        publish(store, project, f"paper{index}.md", ["Methods trial cohort"] * 4)
        for index in range(1, count)
    ]
    identifiers = [others[-1], dominant, *others[:-1]]
    # Ordinary global top six omit every other paper.
    assert {hit["document_id"] for hit in store.search(project, "quantum")} == {dominant}
    sources, coverage = build_comparison_context(store, project, "quantum", identifiers)
    assert Counter(source["document_id"] for source in sources) == dict.fromkeys(identifiers, 3)
    assert [source["source_id"] for source in sources] == [f"K{i}" for i in range(1, 3 * count + 1)]
    assert [row["document_id"] for row in coverage["documents"]] == identifiers
    assert coverage == {
        "task": "compare",
        "documents": [
            {
                "document_id": identifier,
                "name": store.get_document(project, identifier)["name"],
                "source_ids": [f"K{index * 3 + offset}" for offset in (1, 2, 3)],
                "status": "retrieved",
            }
            for index, identifier in enumerate(identifiers)
        ],
    }
    assert build_comparison_context(store, project, "quantum", identifiers) == (sources, coverage)


def test_comparison_uses_full_original_evidence_not_ui_snippets(workspace):
    store, project = workspace
    text = "Methods\n" + "Original detail. " * 75 + "\nResults: n=37; limitations: no control."
    first = publish(store, project, "paper.md", [text])
    second = publish(store, project, "other.md", ["Methods: observational study"])
    ui_snippet = store.search(project, "Methods", [first])[0]
    assert "n=37" not in ui_snippet["text"]
    sources, _ = build_comparison_context(store, project, "Methods", [first, second])
    chunk = store.get_document_chunks(project, first)[0]
    assert sources[0] == {**chunk, "project_id": project, "source_id": "K1"}
    assert sources[0]["text"] == text
    assert 450 < len(text) <= CHUNK_CHARS


@pytest.mark.parametrize("all_missing", [False, True])
def test_no_hit_documents_remain_in_coverage_without_fabricated_sources(workspace, all_missing):
    store, project = workspace
    first = publish(store, project, "missing.md", ["zebra orchard"])
    second = publish(
        store, project, "second.md", ["violet orange" if all_missing else "Results: trial"]
    )
    sources, coverage = build_comparison_context(store, project, "quantum", [first, second])
    assert coverage["documents"][0] == {
        "document_id": first,
        "name": "missing.md",
        "source_ids": [],
        "status": "missing",
    }
    assert len(sources) == (0 if all_missing else 1)
    assert coverage["documents"][1]["source_ids"] == ([] if all_missing else ["K1"])
    assert coverage["documents"][1]["status"] == ("missing" if all_missing else "retrieved")


@pytest.mark.parametrize(
    "query",
    [
        "topic " + " ".join(f"word{i}" for i in range(1500)),
        "topic " + "".join(chr(0x4E00 + i) for i in range(3000)),
        "topic " + "x" * 8000,
    ],
)
def test_bounded_query_keeps_bilingual_structure_inside_token_limit(workspace, monkeypatch, query):
    store, project = workspace
    identifiers = [
        publish(store, project, "english.md", ["limitations"]),
        publish(store, project, "chinese.md", ["局限"]),
    ]
    search = store.search
    called = []

    def capture(project_id, search_query, document_ids, limit):
        called.append((search_query, document_ids, limit))
        assert len(search_query) <= 2000
        tokens = list(dict.fromkeys(search_tokens(search_query)))[:128]
        assert {"abstract", "methods", "results", "limitations", "局限", "样本"} <= set(tokens)
        assert "topic" in tokens
        return search(project_id, search_query, document_ids, limit)

    monkeypatch.setattr(store, "search", capture)
    sources, _ = build_comparison_context(store, project, query, identifiers)
    assert len(sources) == 2
    assert [selection for _, selection, _ in called] == [[identifier] for identifier in identifiers]
    assert all(limit == 3 for _, _, limit in called)


def test_evidence_bound_is_fifteen_full_chunks(workspace):
    store, project = workspace
    text = "Methods " + "x" * (CHUNK_CHARS - 8)
    identifiers = [publish(store, project, f"{index}.md", [text] * 5) for index in range(5)]
    sources, _ = build_comparison_context(store, project, "Methods", identifiers)
    assert len(sources) == 15
    assert sum(len(source["text"]) for source in sources) == 15 * CHUNK_CHARS


def test_stale_and_duplicate_hits_never_become_evidence(workspace, monkeypatch):
    store, project = workspace
    first = publish(store, project, "one.md", ["Methods one"])
    second = publish(store, project, "two.md", ["Methods two"])
    chunk = store.get_document_chunks(project, first)[0]

    def search(project_id, query, document_ids, limit):
        if document_ids == [first]:
            return [{"id": chunk["id"], "text": "truncated"}] * 6
        return [{"id": "deleted", "text": "must not be used"}]

    monkeypatch.setattr(store, "search", search)
    sources, coverage = build_comparison_context(store, project, "Methods", [first, second])
    assert sources == [{**chunk, "project_id": project, "source_id": "K1"}]
    assert coverage["documents"][1]["status"] == "missing"


@pytest.mark.parametrize("identifiers", [[], ["one"], ["one", "one"], list("abcdef")])
def test_builder_rejects_invalid_selection_before_retrieval(workspace, identifiers):
    store, project = workspace
    with pytest.raises(ValueError):
        build_comparison_context(store, project, "Methods", identifiers)


def test_builder_rechecks_scope_and_readiness_before_retrieval(workspace, monkeypatch):
    store, project = workspace
    ready = publish(store, project, "ready.md", ["Methods ready"])
    pending = store.create_document(project, "pending.md", b"Methods pending")["id"]
    other = store.create_project("Other")["id"]
    foreign = publish(store, other, "foreign.md", ["Methods foreign"])

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid selections must not search")

    monkeypatch.setattr(store, "search", forbidden)
    with pytest.raises(ValueError):
        build_comparison_context(store, project, "Methods", [ready, pending])
    with pytest.raises(KeyError):
        build_comparison_context(store, project, "Methods", [pending, foreign])
