from __future__ import annotations

import asyncio
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from api.workspace_store import WorkspaceStore

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 300
MAX_TEXT_CHARS = 2_000_000
MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024
CHUNK_CHARS = 1600
_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]+")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class DocumentError(ValueError):
    pass


def document_name(filename: str) -> tuple[str, str]:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise DocumentError("文件名无效")
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".md"}:
        raise DocumentError("仅支持 PDF 和 Markdown (.md) 文件")
    return name, "pdf" if suffix == ".pdf" else "markdown"


def search_tokens(text: str) -> list[str]:
    tokens = []
    for match in _TOKEN.finditer(unicodedata.normalize("NFKC", text).casefold()):
        word = match.group()
        if word[0].isascii() or len(word) == 1:
            tokens.append(word)
        else:
            tokens.extend(word[index : index + 2] for index in range(len(word) - 1))
    return tokens


def _chunk(text: str, location: str, **locator: Any) -> dict:
    return {
        "text": text,
        "location": location,
        "page": None,
        "line_start": None,
        "line_end": None,
        **locator,
    }


def parse_markdown(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise DocumentError("Markdown 必须使用 UTF-8 编码") from exc
    if len(text) > MAX_TEXT_CHARS:
        raise DocumentError("文档文本超过 200 万字符限制")
    lines = text.splitlines()
    chunks: list[dict] = []
    buffer = ""
    start = end = 0
    heading = ""
    fence = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            location = f"第 {start}–{end} 行"
            if heading:
                location = f"{heading} · {location}"
            chunks.append(_chunk(buffer.strip(), location, line_start=start, line_end=end))
        buffer = ""

    for index, line in enumerate(lines, 1):
        fence_match = _FENCE.match(line)
        heading_match = _HEADING.match(line) if not fence else None
        setext = (
            not fence
            and line.strip()
            and index < len(lines)
            and re.fullmatch(r" {0,3}(?:=+|-+)\s*", lines[index])
        )
        if heading_match or setext:
            flush()
            heading = (heading_match.group(2) if heading_match else line.strip())[:200]
        if fence_match:
            marker = fence_match.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
        piece = line + ("\n" if index < len(lines) else "")
        while piece:
            if not buffer:
                start = index
            amount = min(CHUNK_CHARS - len(buffer), len(piece))
            buffer += piece[:amount]
            piece = piece[amount:]
            end = index
            if len(buffer) == CHUNK_CHARS:
                flush()
    flush()
    if not chunks:
        raise DocumentError("Markdown 没有可索引文本")
    return chunks


def parse_pdf(data: bytes) -> list[dict]:
    from io import BytesIO

    from pypdf import PdfReader, apply_configuration

    chunks = []
    total_chars = 0
    total_stream_bytes = 0
    # Context-local limits also cover object streams and nested form extraction.
    with apply_configuration(
        maximum_declared_stream_length=MAX_UPLOAD_BYTES,
        array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
        zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
        lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
        run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
        brotli_maximum_output_length=MAX_PDF_STREAM_BYTES,
        jbig2_maximum_output_length=MAX_PDF_STREAM_BYTES,
        image_maximum_buffer_size=MAX_PDF_STREAM_BYTES,
        stream_decoding_work_maximum_length=MAX_PDF_STREAM_BYTES * 2,
        stream_parsing_work_maximum_count=200_000,
        page_tree_maximum_entries=MAX_PDF_PAGES * 2 + 2,
        xform_maximum_invocations_per_extraction=100,
        jbig2dec_binary=None,
    ):
        # Publisher PDFs may repeat /MediaBox; parsing limits remain enforced in tolerant mode.
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentError("加密 PDF 无法解析，请上传未加密文件")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentError("PDF 超过 300 页限制")
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            if contents is not None:
                total_stream_bytes += len(contents.get_data())
                if total_stream_bytes > MAX_UPLOAD_BYTES:
                    raise DocumentError("PDF 解压后内容超过限制")
            visited_chars = total_chars

            def visit_text(text: str, *_: Any) -> None:
                nonlocal visited_chars
                visited_chars += len(text)
                if visited_chars > MAX_TEXT_CHARS:
                    raise DocumentError("文档文本超过 200 万字符限制")

            text = page.extract_text(visitor_text=visit_text) or ""
            total_chars += len(text)
            if total_chars > MAX_TEXT_CHARS:
                raise DocumentError("文档文本超过 200 万字符限制")
            for offset in range(0, len(text), CHUNK_CHARS):
                part = text[offset : offset + CHUNK_CHARS].strip()
                if part:
                    chunks.append(_chunk(part, f"第 {number} 页", page=number))
    return chunks


def parse_document(kind: str, data: bytes) -> tuple[str, list[dict]]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("文件超过 20 MiB 限制")
    if kind == "markdown":
        return "ready", parse_markdown(data)
    if kind != "pdf":
        raise DocumentError("不支持的文件类型")
    try:
        chunks = parse_pdf(data)
    except DocumentError:
        raise
    except ImportError as exc:
        raise DocumentError("PDF 解析器不可用，请检查 pypdf 安装版本") from exc
    except Exception as exc:
        raise DocumentError("PDF 无效、损坏或解压内容超过安全限制") from exc
    return ("ready" if chunks else "needs_ocr"), chunks


def _process_document(store: WorkspaceStore, project_id: str, document_id: str) -> None:
    job = store.claim_document(project_id, document_id)
    if job is None:
        return
    generation, kind, _ = job
    try:
        data = store.read_document_job(project_id, document_id, generation)
        if data is None:
            return
        status, chunks = parse_document(kind, data)
        store.publish_document(project_id, document_id, generation, status, chunks)
    except DocumentError as exc:
        store.fail_document(project_id, document_id, generation, str(exc))
    except Exception:
        store.fail_document(project_id, document_id, generation, "文档解析失败，请重试")


async def process_document(store: WorkspaceStore, project_id: str, document_id: str) -> None:
    await asyncio.to_thread(_process_document, store, project_id, document_id)
