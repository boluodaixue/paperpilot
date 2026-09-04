"""Sandboxed, context-bound local file reader used by Research Agents.

The model only sees virtual root names and POSIX relative paths. Host paths are
supplied by the trusted runtime through :func:`file_reader_scope` and never
appear in tool results or errors.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any


__all__ = ["FileReaderError", "FileReaderTool", "ScopedFileRoot", "file_reader_scope"]

_SUPPORTED_EXTS = frozenset(
    {".txt", ".md", ".markdown", ".pdf", ".csv", ".json", ".docx"}
)
_FORMAT_BY_EXT = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".csv": "csv",
    ".json": "json",
    ".docx": "docx",
}
_MAX_FILE_SIZE = 10 * 1024 * 1024
_MAX_OUTPUT_CHARS = 12_000
_MAX_PDF_PAGES = 100
_MAX_CSV_ROWS = 20
_MAX_CSV_COLUMNS = 100
_MAX_CSV_CELL_CHARS = 2_000
_MAX_DOCX_ENTRIES = 2_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
_MAX_DOCX_PARAGRAPHS = 2_000
_ROOT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_INVALID_WINDOWS_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True)
class _AuthorizedRoot:
    path: Path
    identity: tuple[int, int, int]
    prefix_parts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopedFileRoot:
    """Trusted host root with a fixed virtual subdirectory boundary."""

    path: str | os.PathLike[str]
    prefix: str = ""


_EMPTY_ROOTS: Mapping[str, _AuthorizedRoot] = MappingProxyType({})
_ACTIVE_ROOTS: ContextVar[Mapping[str, _AuthorizedRoot]] = ContextVar(
    "paperpilot_file_reader_roots",
    default=_EMPTY_ROOTS,
)


class FileReaderError(RuntimeError):
    """A safe, user-explainable rejection which never contains a host path."""


def _is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a symlink, junction, or other reparse point."""
    try:
        details = os.lstat(path)
    except OSError:
        return False
    attributes = int(getattr(details, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    return bool(
        stat.S_ISLNK(details.st_mode)
        or is_junction(path)
        or attributes & reparse_flag
    )


def _normalize_roots(
    allowed_roots: Mapping[str, str | os.PathLike[str] | ScopedFileRoot] | None,
) -> Mapping[str, _AuthorizedRoot]:
    if allowed_roots is None:
        return _EMPTY_ROOTS
    if not isinstance(allowed_roots, Mapping):
        raise TypeError("allowed_roots must be a mapping or None")

    normalized: dict[str, _AuthorizedRoot] = {}
    for raw_name, raw_spec in allowed_roots.items():
        if not isinstance(raw_name, str) or not _ROOT_NAME.fullmatch(raw_name):
            raise ValueError("file reader root names must be safe lowercase identifiers")
        raw_path = raw_spec.path if isinstance(raw_spec, ScopedFileRoot) else raw_spec
        if not isinstance(raw_path, (str, os.PathLike)):
            raise TypeError("file reader roots must be path-like")
        prefix_parts: tuple[str, ...] = ()
        if isinstance(raw_spec, ScopedFileRoot) and raw_spec.prefix:
            _, prefix_parts = _validate_relative_path(raw_spec.prefix)
        lexical_root = Path(raw_path)
        if not lexical_root.exists() or not lexical_root.is_dir():
            raise FileReaderError(f"authorized root {raw_name!r} is unavailable")
        if _is_reparse_point(lexical_root):
            raise FileReaderError(f"authorized root {raw_name!r} cannot be a link")
        try:
            resolved_root = lexical_root.resolve(strict=True)
            details = os.lstat(resolved_root)
        except OSError as exc:
            raise FileReaderError(f"authorized root {raw_name!r} is unavailable") from exc
        if not stat.S_ISDIR(details.st_mode) or _is_reparse_point(resolved_root):
            raise FileReaderError(f"authorized root {raw_name!r} cannot be a link")
        normalized[raw_name] = _AuthorizedRoot(
            path=resolved_root,
            identity=_identity(details),
            prefix_parts=prefix_parts,
        )
    return MappingProxyType(dict(sorted(normalized.items()))) if normalized else _EMPTY_ROOTS


@contextmanager
def file_reader_scope(
    allowed_roots: Mapping[str, str | os.PathLike[str] | ScopedFileRoot] | None,
) -> Iterator[None]:
    """Bind immutable named roots to the current synchronous/async context.

    Context variables are copied per asyncio task, so overlapping research runs
    cannot mutate or observe one another's grants. Missing, ``None``, and empty
    scopes are deny-all.
    """
    token = _ACTIVE_ROOTS.set(_normalize_roots(allowed_roots))
    try:
        yield
    finally:
        _ACTIVE_ROOTS.reset(token)


def _virtual_path(root: str, relative: str) -> str:
    return f"{root}/{relative}" if root and relative else "requested file"


def _validate_relative_path(value: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FileReaderError("file path must be a non-empty relative POSIX path")
    if "\\" in value:
        raise FileReaderError("file path must use forward slashes")
    if PurePosixPath(value).is_absolute():
        raise FileReaderError("absolute file paths are not allowed")
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or value.startswith("//"):
        raise FileReaderError("absolute file paths are not allowed")

    parts = tuple(value.split("/"))
    for component in parts:
        if component in {"", ".", ".."}:
            raise FileReaderError("file path contains an unsafe component")
        if component.endswith((" ", ".")):
            raise FileReaderError("file path contains an unsafe component")
        if any(
            character in _INVALID_WINDOWS_PATH_CHARACTERS
            or ord(character) < 32
            or ord(character) == 127
            for character in component
        ):
            raise FileReaderError("file path contains an unsafe component")
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise FileReaderError("file path contains an unsafe component")
    return PurePosixPath(value).as_posix(), parts


def _identity(details: os.stat_result) -> tuple[int, int, int]:
    return (int(details.st_dev), int(details.st_ino), stat.S_IFMT(details.st_mode))


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        *_identity(details),
        int(details.st_size),
        int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1_000_000_000))),
    )


def _snapshot_components(
    root: Path,
    parts: tuple[str, ...],
    virtual: str,
) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    snapshots: list[tuple[Path, tuple[int, int, int]]] = []
    current = root
    for component in parts:
        current = current / component
        try:
            details = os.lstat(current)
        except FileNotFoundError as exc:
            raise FileReaderError(f"file not found: {virtual}") from exc
        except OSError as exc:
            raise FileReaderError(f"file is unavailable: {virtual}") from exc
        if _is_reparse_point(current):
            raise FileReaderError(f"linked paths are not allowed: {virtual}")
        snapshots.append((current, _identity(details)))
    return tuple(snapshots)


def _check_authorized_root(root: _AuthorizedRoot, virtual: str) -> None:
    try:
        details = os.lstat(root.path)
    except OSError as exc:
        raise FileReaderError(f"authorized root changed: {virtual}") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or _is_reparse_point(root.path)
        or _identity(details) != root.identity
    ):
        raise FileReaderError(f"authorized root changed: {virtual}")


def _read_bounded_snapshot(
    authorized_root: _AuthorizedRoot,
    parts: tuple[str, ...],
    virtual: str,
    max_file_size: int,
) -> bytes:
    """Read one identity-checked byte snapshot from one OS descriptor."""
    _check_authorized_root(authorized_root, virtual)
    root = authorized_root.path
    component_snapshots = _snapshot_components(
        root,
        (*authorized_root.prefix_parts, *parts),
        virtual,
    )
    target = component_snapshots[-1][0]
    try:
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FileReaderError(f"file escapes its authorized root: {virtual}") from exc

    try:
        before = os.lstat(target)
    except OSError as exc:
        raise FileReaderError(f"file is unavailable: {virtual}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise FileReaderError(f"not a regular file: {virtual}")
    if before.st_size > max_file_size:
        raise FileReaderError(f"file exceeds the size limit: {virtual}")

    flags = os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_CLOEXEC", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FileReaderError(f"not a regular file: {virtual}")
        if _identity(opened) != _identity(before):
            raise FileReaderError(f"file changed while being authorized: {virtual}")
        if opened.st_size > max_file_size:
            raise FileReaderError(f"file exceeds the size limit: {virtual}")

        chunks: list[bytes] = []
        remaining = max_file_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_file_size:
            raise FileReaderError(f"file exceeds the size limit: {virtual}")
        after_open = os.fstat(descriptor)
        if _file_identity(after_open) != _file_identity(opened):
            raise FileReaderError(f"file changed while being read: {virtual}")
    except FileReaderError:
        raise
    except OSError as exc:
        raise FileReaderError(f"file is unavailable: {virtual}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    for component_path, expected in component_snapshots:
        try:
            details = os.lstat(component_path)
        except OSError as exc:
            raise FileReaderError(f"file changed while being read: {virtual}") from exc
        if _is_reparse_point(component_path) or _identity(details) != expected:
            raise FileReaderError(f"file changed while being read: {virtual}")
    try:
        current = os.lstat(target)
    except OSError as exc:
        raise FileReaderError(f"file changed while being read: {virtual}") from exc
    if _file_identity(current) != _file_identity(before):
        raise FileReaderError(f"file changed while being read: {virtual}")
    _check_authorized_root(authorized_root, virtual)
    return payload


def _decode_utf8(payload: bytes, virtual: str) -> str:
    try:
        return payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise FileReaderError(f"file is not valid UTF-8 text: {virtual}") from exc


def _truncate(content: str, maximum: int) -> tuple[str, bool]:
    if len(content) <= maximum:
        return content, False
    return content[:maximum], True


def _read_pdf(
    payload: bytes,
    virtual: str,
    *,
    max_pages: int,
    max_chars: int,
) -> tuple[str, bool]:
    if b"%PDF-" not in payload[:1024]:
        raise FileReaderError(f"file does not contain a valid PDF header: {virtual}")

    texts: list[str] = []
    used = 0
    truncated = False

    def add_page(page_number: int, text: str | None) -> bool:
        nonlocal used, truncated
        if not text:
            return True
        block = f"--- Page {page_number} ---\n{text}\n\n"
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            return False
        if len(block) > remaining:
            texts.append(block[:remaining])
            used = max_chars
            truncated = True
            return False
        texts.append(block)
        used += len(block)
        return True

    try:
        try:
            import pdfplumber
        except ImportError:
            pdfplumber = None
        if pdfplumber is not None:
            with pdfplumber.open(io.BytesIO(payload)) as document:
                truncated = len(document.pages) > max_pages
                for number, page in enumerate(document.pages[:max_pages], 1):
                    if not add_page(number, page.extract_text()):
                        break
            return "".join(texts).rstrip(), truncated

        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FileReaderError("PDF reading support is unavailable") from exc
        document = PdfReader(io.BytesIO(payload))
        truncated = len(document.pages) > max_pages
        for number, page in enumerate(document.pages[:max_pages], 1):
            if not add_page(number, page.extract_text()):
                break
        return "".join(texts).rstrip(), truncated
    except FileReaderError:
        raise
    except Exception as exc:
        raise FileReaderError(f"failed to parse PDF file: {virtual}") from exc


def _read_csv(
    payload: bytes,
    virtual: str,
    *,
    max_rows: int,
    max_chars: int,
) -> tuple[str, bool]:
    text = _decode_utf8(payload, virtual)
    reader = csv.reader(io.StringIO(text, newline=""))
    lines: list[str] = []
    truncated = False
    try:
        for index, row in enumerate(reader):
            if index >= max_rows:
                truncated = True
                break
            if len(row) > _MAX_CSV_COLUMNS:
                row = row[:_MAX_CSV_COLUMNS]
                truncated = True
            bounded = []
            for cell in row:
                if len(cell) > _MAX_CSV_CELL_CHARS:
                    truncated = True
                bounded.append(cell[:_MAX_CSV_CELL_CHARS])
            rendered = io.StringIO()
            csv.writer(rendered, lineterminator="").writerow(bounded)
            lines.append(rendered.getvalue())
    except (csv.Error, UnicodeError) as exc:
        raise FileReaderError(f"failed to parse CSV file: {virtual}") from exc
    content, output_truncated = _truncate("\n".join(lines), max_chars)
    return content, truncated or output_truncated


def _json_summary(value: Any, *, depth: int = 0, max_depth: int = 3) -> str:
    if depth > max_depth:
        return "..."
    if isinstance(value, dict):
        entries = [
            f"  {str(key)[:300]}: {_json_summary(item, depth=depth + 1, max_depth=max_depth)}"
            for key, item in list(value.items())[:10]
        ]
        if len(value) > 10:
            entries.append(f"  ... ({len(value) - 10} more keys)")
        return "{\n" + "\n".join(entries) + "\n}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return (
            f"[{len(value)} items, e.g.: "
            f"{_json_summary(value[0], depth=depth + 1, max_depth=max_depth)}]"
        )
    return repr(value)[:2_000]


def _read_json(
    payload: bytes,
    virtual: str,
    *,
    max_chars: int,
) -> tuple[str, bool]:
    try:
        value = json.loads(_decode_utf8(payload, virtual))
    except FileReaderError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FileReaderError(f"failed to parse JSON file: {virtual}") from exc
    return _truncate(_json_summary(value), max_chars)


def _read_docx(
    payload: bytes,
    virtual: str,
    *,
    max_paragraphs: int,
    max_chars: int,
) -> tuple[str, bool]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_DOCX_ENTRIES:
                raise FileReaderError(f"DOCX archive has too many entries: {virtual}")
            total_size = sum(entry.file_size for entry in entries)
            if total_size > _MAX_DOCX_UNCOMPRESSED_BYTES:
                raise FileReaderError(f"DOCX archive expands beyond the limit: {virtual}")
    except FileReaderError:
        raise
    except (zipfile.BadZipFile, OSError) as exc:
        raise FileReaderError(f"failed to parse DOCX file: {virtual}") from exc

    try:
        from docx import Document
    except ImportError as exc:
        raise FileReaderError("DOCX reading support is unavailable") from exc
    try:
        document = Document(io.BytesIO(payload))
        paragraphs: list[str] = []
        truncated = False
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            if len(paragraphs) >= max_paragraphs:
                truncated = True
                break
            paragraphs.append(text)
        content, output_truncated = _truncate("\n\n".join(paragraphs), max_chars)
        return content, truncated or output_truncated
    except Exception as exc:
        raise FileReaderError(f"failed to parse DOCX file: {virtual}") from exc


class FileReaderTool:
    """Read a bounded file snapshot from the current trusted virtual roots."""

    name: str = "file_reader"
    description: str = (
        "Read a file from an authorized virtual root. Provide the root name and "
        "a relative POSIX path. Absolute paths and path traversal are forbidden. "
        "For root 'memory', omit the Vault 'Memories/<memory-id>/' prefix (for "
        "example, use 'notes/N-example.md'). "
        "For root 'artifact', provide the artifact filename from a receipt. "
        "Supports: .txt, .md, .markdown, .pdf, .csv, .json, .docx."
    )

    def __init__(
        self,
        *,
        max_file_size: int = _MAX_FILE_SIZE,
        max_output_chars: int = _MAX_OUTPUT_CHARS,
        max_pdf_pages: int = _MAX_PDF_PAGES,
        max_csv_rows: int = _MAX_CSV_ROWS,
        max_docx_paragraphs: int = _MAX_DOCX_PARAGRAPHS,
    ) -> None:
        limits = {
            "max_file_size": max_file_size,
            "max_output_chars": max_output_chars,
            "max_pdf_pages": max_pdf_pages,
            "max_csv_rows": max_csv_rows,
            "max_docx_paragraphs": max_docx_paragraphs,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in limits.values()
        ):
            raise ValueError("file reader limits must be positive integers")
        self.max_file_size = max_file_size
        self.max_output_chars = max_output_chars
        self.max_pdf_pages = max_pdf_pages
        self.max_csv_rows = max_csv_rows
        self.max_docx_paragraphs = max_docx_paragraphs

    def is_available(self) -> bool:
        """Return whether this execution context has at least one trusted root."""
        return bool(_ACTIVE_ROOTS.get())

    def get_openai_tool_schema(self) -> dict[str, Any]:
        roots = tuple(_ACTIVE_ROOTS.get())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root": {
                            "type": "string",
                            "enum": list(roots),
                            "description": "Authorized virtual root name",
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Relative POSIX path inside the selected virtual root; "
                                "for root 'memory', omit the Memories/<memory-id>/ prefix; "
                                "for root 'artifact', use the receipt filename"
                            ),
                        },
                    },
                    "required": ["root", "path"],
                    "additionalProperties": False,
                },
            },
        }

    async def execute(self, *, root: str, path: str) -> dict[str, Any]:
        roots = _ACTIVE_ROOTS.get()
        if not isinstance(root, str) or root not in roots:
            raise FileReaderError("requested file root is not authorized")
        relative, parts = _validate_relative_path(path)
        virtual = _virtual_path(root, relative)
        extension = PurePosixPath(relative).suffix.lower()
        if extension not in _SUPPORTED_EXTS:
            raise FileReaderError(f"unsupported file type: {virtual}")

        payload = await asyncio.to_thread(
            _read_bounded_snapshot,
            roots[root],
            parts,
            virtual,
            self.max_file_size,
        )
        if root == "artifact" and extension == ".json":
            content, truncated = _truncate(
                _decode_utf8(payload, virtual),
                self.max_output_chars,
            )
        else:
            content, truncated = await asyncio.to_thread(
                self._parse,
                payload,
                extension,
                virtual,
            )
        result = {
            "path": virtual,
            "format": _FORMAT_BY_EXT[extension],
            "content": content,
            "truncated": truncated,
        }
        if root == "artifact":
            result["content_hash"] = hashlib.sha256(payload).hexdigest()
        return result

    def _parse(
        self,
        payload: bytes,
        extension: str,
        virtual: str,
    ) -> tuple[str, bool]:
        try:
            if extension in {".txt", ".md", ".markdown"}:
                return _truncate(_decode_utf8(payload, virtual), self.max_output_chars)
            if extension == ".pdf":
                return _read_pdf(
                    payload,
                    virtual,
                    max_pages=self.max_pdf_pages,
                    max_chars=self.max_output_chars,
                )
            if extension == ".csv":
                return _read_csv(
                    payload,
                    virtual,
                    max_rows=self.max_csv_rows,
                    max_chars=self.max_output_chars,
                )
            if extension == ".json":
                return _read_json(payload, virtual, max_chars=self.max_output_chars)
            if extension == ".docx":
                return _read_docx(
                    payload,
                    virtual,
                    max_paragraphs=self.max_docx_paragraphs,
                    max_chars=self.max_output_chars,
                )
        except FileReaderError:
            raise
        except Exception as exc:
            raise FileReaderError(f"failed to parse file: {virtual}") from exc
        raise FileReaderError(f"unsupported file type: {virtual}")
