"""Filesystem-backed Markdown implementation of PaperPilot's one Memory Store."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from .models import (
    ExecutionIdentity,
    MemoryDescriptor,
    MemoryImportDuplicate,
    MemoryImportProposal,
    MemoryManifest,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchResult,
)
from .rendering import (
    managed_note_id,
    render_evidence_note,
    render_memory_home,
    render_report,
    render_v2_report,
    render_source_note,
    report_note_id,
    safe_note_id,
    source_note_id,
)
from .vault import (
    LEGACY_MEMORY_ID,
    build_attachment_wikilink,
    build_wikilink,
    memory_relative_path,
    resolve_memory_attachment_path,
    resolve_vault_markdown_path,
    scan_legacy_memory_markdown,
    validate_frontmatter,
    validate_memory_attachment_path,
    validate_memory_descriptor,
    validate_memory_id,
    validate_wikilink_target,
)


_MEMORY_DIRECTORIES = (
    "reports",
    "evidence",
    "sources",
    "notes",
    "imports",
    "attachments",
)
_HOME_NOTES_HEADING = re.compile(r"^## Notes\s*$")
_HOME_IMPORTS_HEADING = re.compile(r"^## Imports\s*$")
_HOME_SECTION_HEADING = re.compile(r"^##\s+")
_WIKILINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_ID = re.compile(r"^Import-[0-9a-f]{32}$")
_IMPORT_NOTE_ID = re.compile(r"^Note-import-[0-9a-f]{32}$")
_IMPORT_MEDIA_EXTENSIONS = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/html": "html",
}
_IMPORT_FRONTMATTER_FIELDS = frozenset(
    {
        "id",
        "type",
        "memory_id",
        "title",
        "created_at",
        "updated_at",
        "origin",
        "status",
        "tags",
        "source_kind",
        "source_ref",
        "locator",
        "media_type",
        "byte_size",
        "content_hash",
        "attachment_path",
    }
)
_IMPORT_NOTE_FRONTMATTER_FIELDS = frozenset(
    {
        "id",
        "type",
        "memory_id",
        "title",
        "created_at",
        "updated_at",
        "origin",
        "status",
        "tags",
    }
)
_MAX_HOME_RESTORE_EXCHANGES = 16
_LEGACY_MIGRATION_PROPOSAL_KEYS = frozenset(
    {
        "proposal_id",
        "source_memory_id",
        "source_content_hash",
        "target_memory_id",
        "title",
        "created_at",
        "target_relative_path",
        "home_path",
        "home_markdown",
        "files",
    }
)
_VAULT_LOCKS: dict[str, threading.RLock] = {}
_VAULT_LOCKS_GUARD = threading.Lock()


class MemoryWriteConflictError(RuntimeError):
    """The Vault changed after a controlled Memory proposal was prepared."""


def _import_fingerprint(source_ref: str, locator: str, content_hash: str) -> str:
    canonical = json.dumps(
        {
            "source_ref": source_ref,
            "locator": locator,
            "content_hash": content_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _windows_replace_file(
    target: Path,
    replacement: Path,
    backup: Path | None,
) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    replace_file.restype = ctypes.c_int
    succeeded = replace_file(
        str(target),
        str(replacement),
        str(backup) if backup is not None else None,
        0,
        None,
        None,
    )
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_exchange(first: Path, second: Path) -> None:
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(first),
            -100,
            os.fsencode(second),
            2,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "renamex_np is unavailable")
        renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(first), os.fsencode(second), 2)
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return
    raise OSError(errno.ENOTSUP, "atomic file exchange is unavailable")


def _atomic_replace_preserving_old(target: Path, replacement: Path) -> Path:
    """Replace target atomically and return the old target at a private path."""
    if os.name == "nt":
        backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
        _windows_replace_file(target, replacement, backup)
        return backup
    _atomic_exchange(target, replacement)
    return replacement


def _discard_preserved(path: Path) -> None:
    try:
        path.unlink()
    except OSError as first_error:
        try:
            os.unlink(path)
        except OSError:
            raise first_error


def _frontmatter_parts(
    markdown: str,
    *,
    label: str,
) -> tuple[list[str], int, dict[str, object]]:
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError(f"{label} must start with YAML frontmatter")
    closing_indexes = [
        index
        for index, line in enumerate(lines[1:], start=1)
        if line.rstrip("\r\n") == "---"
    ]
    if not closing_indexes:
        raise ValueError(f"{label} frontmatter is not closed")
    closing = closing_indexes[0]
    yaml_text = "".join(lines[1:closing])
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{label} frontmatter is invalid YAML") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{label} frontmatter must be a mapping")
    return lines, closing, validate_frontmatter(loaded)


def _parse_wikilink(wikilink: str) -> tuple[str, str | None]:
    if not isinstance(wikilink, str):
        raise ValueError("wikilink must be a string")
    match = _WIKILINK.fullmatch(wikilink)
    if match is None:
        raise ValueError("wikilink must contain exactly one complete WikiLink")
    parts = match.group(1).split("|", 1)
    target = validate_wikilink_target(parts[0])
    alias = parts[1] if len(parts) == 2 else None
    if build_wikilink(f"{target}.md", alias) != wikilink:
        raise ValueError("wikilink must use canonical target and alias syntax")
    return target, alias


def _parse_attachment_wikilink(wikilink: str) -> tuple[str, str | None]:
    if not isinstance(wikilink, str):
        raise ValueError("attachment wikilink must be a string")
    match = _WIKILINK.fullmatch(wikilink)
    if match is None:
        raise ValueError("attachment wikilink must contain exactly one WikiLink")
    parts = match.group(1).split("|", 1)
    target = validate_memory_attachment_path(parts[0])
    alias = parts[1] if len(parts) == 2 else None
    if build_attachment_wikilink(target, alias) != wikilink:
        raise ValueError("attachment wikilink must use canonical target and alias syntax")
    return target, alias


def update_memory_home_with_note(
    home_markdown: str,
    wikilink: str,
    updated_at: str,
) -> str:
    """Update only Home frontmatter time and its unique Notes list."""
    if not isinstance(home_markdown, str) or not home_markdown:
        raise ValueError("home_markdown must be a non-empty string")
    link_target, _ = _parse_wikilink(wikilink)
    lines, closing, frontmatter = _frontmatter_parts(home_markdown, label="Home.md")
    if frontmatter["type"] != "home":
        raise ValueError("Home.md frontmatter type must be 'home'")
    validate_frontmatter({**frontmatter, "updated_at": updated_at})

    updated_lines = [
        index
        for index, line in enumerate(lines[1:closing], start=1)
        if line.startswith("updated_at:")
    ]
    if len(updated_lines) != 1:
        raise ValueError("Home.md must contain exactly one updated_at property")

    note_headings = [
        index
        for index, line in enumerate(lines[closing + 1 :], start=closing + 1)
        if _HOME_NOTES_HEADING.fullmatch(line.rstrip("\r\n"))
    ]
    if len(note_headings) != 1:
        raise ValueError("Home.md must contain exactly one ## Notes section")

    existing_targets: set[str] = set()
    for match in _WIKILINK.finditer(home_markdown):
        existing_targets.add(match.group(1).split("|", 1)[0].strip())
    if link_target in existing_targets:
        raise ValueError("Home.md already contains the note WikiLink")

    newline = "\r\n" if "\r\n" in home_markdown else "\n"
    updated_index = updated_lines[0]
    updated_ending = (
        "\r\n"
        if lines[updated_index].endswith("\r\n")
        else "\n" if lines[updated_index].endswith("\n") else ""
    )
    lines[updated_index] = (
        f"updated_at: {json.dumps(updated_at, ensure_ascii=False)}{updated_ending}"
    )

    heading = note_headings[0]
    section_end = len(lines)
    for index in range(heading + 1, len(lines)):
        if _HOME_SECTION_HEADING.match(lines[index].rstrip("\r\n")):
            section_end = index
            break
    section = [
        line
        for line in lines[heading + 1 : section_end]
        if line.strip() != "- None yet."
    ]
    trailing_start = len(section)
    while trailing_start and not section[trailing_start - 1].strip():
        trailing_start -= 1
    if trailing_start == 0:
        section = [newline, f"- {wikilink}{newline}", newline]
    else:
        core = section[:trailing_start]
        trailing = section[trailing_start:] or [newline]
        if core[-1].strip() and not core[-1].lstrip().startswith("-"):
            core.append(newline)
        section = [*core, f"- {wikilink}{newline}", *trailing]
    lines[heading + 1 : section_end] = section
    return "".join(lines)


def _append_home_section_link(
    lines: list[str],
    *,
    closing: int,
    heading_pattern: re.Pattern[str],
    section_name: str,
    wikilink: str,
    newline: str,
) -> None:
    link_target, _ = _parse_wikilink(wikilink)
    headings = [
        index
        for index, line in enumerate(lines[closing + 1 :], start=closing + 1)
        if heading_pattern.fullmatch(line.rstrip("\r\n"))
    ]
    if len(headings) != 1:
        raise ValueError(f"Home.md must contain exactly one ## {section_name} section")
    for match in _WIKILINK.finditer("".join(lines)):
        existing_target = match.group(1).split("|", 1)[0].strip()
        if existing_target == link_target:
            raise ValueError("Home.md already contains the proposed WikiLink")

    heading = headings[0]
    section_end = len(lines)
    for index in range(heading + 1, len(lines)):
        if _HOME_SECTION_HEADING.match(lines[index].rstrip("\r\n")):
            section_end = index
            break
    section = [
        line
        for line in lines[heading + 1 : section_end]
        if line.strip() != "- None yet."
    ]
    trailing_start = len(section)
    while trailing_start and not section[trailing_start - 1].strip():
        trailing_start -= 1
    if trailing_start == 0:
        section = [newline, f"- {wikilink}{newline}", newline]
    else:
        core = section[:trailing_start]
        trailing = section[trailing_start:] or [newline]
        if core[-1].strip() and not core[-1].lstrip().startswith("-"):
            core.append(newline)
        section = [*core, f"- {wikilink}{newline}", *trailing]
    lines[heading + 1 : section_end] = section


def update_memory_home_with_import(
    home_markdown: str,
    import_wikilink: str,
    note_wikilink: str,
    updated_at: str,
) -> str:
    """Update Home Imports and Notes in one deterministic proposal."""
    if not isinstance(home_markdown, str) or not home_markdown:
        raise ValueError("home_markdown must be a non-empty string")
    lines, closing, frontmatter = _frontmatter_parts(home_markdown, label="Home.md")
    if frontmatter["type"] != "home":
        raise ValueError("Home.md frontmatter type must be 'home'")
    validate_frontmatter({**frontmatter, "updated_at": updated_at})
    updated_lines = [
        index
        for index, line in enumerate(lines[1:closing], start=1)
        if line.startswith("updated_at:")
    ]
    if len(updated_lines) != 1:
        raise ValueError("Home.md must contain exactly one updated_at property")

    newline = "\r\n" if "\r\n" in home_markdown else "\n"
    updated_index = updated_lines[0]
    updated_ending = (
        "\r\n"
        if lines[updated_index].endswith("\r\n")
        else "\n" if lines[updated_index].endswith("\n") else ""
    )
    lines[updated_index] = (
        f"updated_at: {json.dumps(updated_at, ensure_ascii=False)}{updated_ending}"
    )
    _append_home_section_link(
        lines,
        closing=closing,
        heading_pattern=_HOME_IMPORTS_HEADING,
        section_name="Imports",
        wikilink=import_wikilink,
        newline=newline,
    )
    _append_home_section_link(
        lines,
        closing=closing,
        heading_pattern=_HOME_NOTES_HEADING,
        section_name="Notes",
        wikilink=note_wikilink,
        newline=newline,
    )
    return "".join(lines)


def _legacy_markdown_parts(
    markdown: str,
    *,
    source_path: str,
) -> tuple[dict[str, object], str]:
    """Split permissive legacy frontmatter without treating it as managed data."""
    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, markdown
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        raise ValueError(f"legacy Markdown frontmatter is not closed: {source_path}")
    try:
        loaded = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"legacy Markdown frontmatter is invalid YAML: {source_path}"
        ) from exc
    if loaded is None:
        frontmatter: dict[str, object] = {}
    elif isinstance(loaded, dict):
        frontmatter = dict(loaded)
    else:
        raise ValueError(
            f"legacy Markdown frontmatter must be a mapping: {source_path}"
        )
    return frontmatter, "".join(lines[closing + 1 :])


def _legacy_snapshot(
    vault_root: Path,
) -> tuple[tuple[dict[str, object], ...], str]:
    """Capture the exact safe Markdown source set without writing the Vault."""
    records: list[dict[str, object]] = []
    digest = hashlib.sha256()
    for relative_path, path in scan_legacy_memory_markdown(vault_root):
        try:
            with path.open("rb") as handle:
                content = handle.read()
                modified_ns = os.fstat(handle.fileno()).st_mtime_ns
        except OSError as exc:
            raise ValueError(
                f"legacy Markdown cannot be read: {relative_path}"
            ) from exc
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"legacy Markdown must be UTF-8: {relative_path}"
            ) from exc
        content_hash = hashlib.sha256(content).hexdigest()
        encoded_path = relative_path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(bytes.fromhex(content_hash))
        records.append(
            {
                "source_path": relative_path,
                "source_content_hash": content_hash,
                "modified_ns": modified_ns,
                "markdown": markdown,
            }
        )
    if not records:
        raise FileNotFoundError("legacy Memory contains no Markdown files")
    return tuple(records), digest.hexdigest()


def _migration_timestamp(modified_ns: object) -> str:
    if not isinstance(modified_ns, int) or isinstance(modified_ns, bool):
        raise TypeError("legacy Markdown modified_ns must be an integer")
    return datetime.fromtimestamp(
        modified_ns / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")


def _legacy_note_title(
    frontmatter: Mapping[str, object],
    body: str,
    *,
    fallback: str,
) -> str:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return heading.group(1).strip() if heading else fallback


def _render_migrated_frontmatter(
    *,
    note_id: str,
    note_type: str,
    memory_id: str,
    title: str,
    timestamp: str,
    root_thread_id: object = None,
) -> str:
    fields: list[tuple[str, object]] = [
        ("id", note_id),
        ("type", note_type),
        ("memory_id", memory_id),
        ("title", title),
        ("created_at", timestamp),
        ("updated_at", timestamp),
        ("origin", "research"),
        ("status", "confirmed"),
    ]
    if root_thread_id is not None:
        if not isinstance(root_thread_id, (str, int, float, bool)):
            raise ValueError("legacy root_thread_id must be a flat YAML scalar")
        fields.append(("root_thread_id", root_thread_id))
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields
    )
    lines.extend(("tags:", "  - paperpilot", "---"))
    return "\n".join(lines)


def _migration_target_map(
    records: tuple[dict[str, object], ...],
    memory_id: str,
) -> dict[str, str]:
    prefixes = {"reports": "Report", "evidence": "Evidence", "sources": "Source"}
    targets: dict[str, str] = {}
    used_targets: dict[str, str] = {}
    for record in records:
        source_path = str(record["source_path"])
        directory = PurePosixPath(source_path).parts[0]
        frontmatter, _ = _legacy_markdown_parts(
            str(record["markdown"]),
            source_path=source_path,
        )
        declared_type = frontmatter.get("type")
        expected_type = directory.removesuffix("s")
        if declared_type is not None and declared_type != expected_type:
            raise ValueError(
                f"legacy Markdown type does not match its directory: {source_path}"
            )
        raw_id = frontmatter.get("id")
        identity = (
            raw_id.strip()
            if isinstance(raw_id, str) and raw_id.strip()
            else PurePosixPath(source_path).stem
        )
        note_id = managed_note_id(prefixes[directory], identity)
        target_path = f"Memories/{memory_id}/{directory}/{note_id}.md"
        portable_target_key = target_path.casefold()
        previous = used_targets.get(portable_target_key)
        if previous is not None:
            raise ValueError(
                "legacy migration target collision: "
                f"{previous} and {source_path} both map to {target_path}"
            )
        used_targets[portable_target_key] = source_path
        targets[source_path] = target_path
    return targets


def _rewrite_legacy_wikilinks(
    body: str,
    *,
    source_path: str,
    target_map: Mapping[str, str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        parts = raw.split("|", 1)
        target = parts[0].strip()
        alias = parts[1] if len(parts) == 2 else None
        if not target or "#" in target or "\\" in target:
            raise ValueError(f"legacy WikiLink is not migratable in {source_path}")
        if target.startswith("Memories/"):
            canonical = validate_wikilink_target(target.removesuffix(".md"))
            return build_wikilink(f"{canonical}.md", alias)
        candidate = target if target.endswith(".md") else f"{target}.md"
        try:
            legacy_target = PurePosixPath(candidate).as_posix()
            # The target map itself is the authoritative set of safe source paths.
            migrated_target = target_map[legacy_target]
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"legacy WikiLink target is missing or ambiguous in {source_path}: {target}"
            ) from exc
        return build_wikilink(migrated_target, alias)

    rewritten = _WIKILINK.sub(replace, body)
    unmatched = _WIKILINK.sub("", body)
    if "[[" in unmatched or "]]" in unmatched:
        raise ValueError(f"legacy Markdown contains malformed WikiLink syntax: {source_path}")
    return rewritten


def _render_migration_home(
    *,
    memory_id: str,
    title: str,
    timestamp: str,
    report_paths: tuple[str, ...],
) -> str:
    home = render_memory_home(
        memory_id=memory_id,
        title=title,
        created_at=timestamp,
        updated_at=timestamp,
    )
    marker = "## Reports\n\n- None yet.\n"
    if home.count(marker) != 1:
        raise RuntimeError("Memory Home renderer no longer has one Reports placeholder")
    if not report_paths:
        return home
    links = "\n".join(f"- {build_wikilink(path)}" for path in report_paths)
    return home.replace(marker, f"## Reports\n\n{links}\n", 1)


def _build_legacy_migration_proposal(
    records: tuple[dict[str, object], ...],
    source_hash: str,
    *,
    proposal_id: str,
    memory_id: str,
    title: str,
    created_at: str,
) -> dict[str, object]:
    target_map = _migration_target_map(records, memory_id)
    files: list[dict[str, str]] = []
    for record in records:
        source_path = str(record["source_path"])
        target_path = target_map[source_path]
        directory = PurePosixPath(source_path).parts[0]
        note_type = directory.removesuffix("s")
        frontmatter, body = _legacy_markdown_parts(
            str(record["markdown"]),
            source_path=source_path,
        )
        note_id = PurePosixPath(target_path).stem
        note_title = _legacy_note_title(
            frontmatter,
            body,
            fallback=PurePosixPath(source_path).stem,
        )
        timestamp = _migration_timestamp(record["modified_ns"])
        rewritten_body = _rewrite_legacy_wikilinks(
            body,
            source_path=source_path,
            target_map=target_map,
        )
        migrated = (
            _render_migrated_frontmatter(
                note_id=note_id,
                note_type=note_type,
                memory_id=memory_id,
                title=note_title,
                timestamp=timestamp,
                root_thread_id=(
                    frontmatter.get("root_thread_id")
                    if note_type == "report"
                    else None
                ),
            )
            + "\n"
            + rewritten_body.lstrip("\r\n")
        )
        if not migrated.endswith("\n"):
            migrated += "\n"
        files.append(
            {
                "source_path": source_path,
                "source_content_hash": str(record["source_content_hash"]),
                "target_path": target_path,
                "markdown": migrated,
                "wikilink": build_wikilink(target_path),
            }
        )
    files.sort(key=lambda item: item["source_path"])
    report_paths = tuple(
        item["target_path"]
        for item in files
        if PurePosixPath(item["target_path"]).parts[2] == "reports"
    )
    home_path = f"Memories/{memory_id}/Home.md"
    home = _render_migration_home(
        memory_id=memory_id,
        title=title,
        timestamp=created_at,
        report_paths=report_paths,
    )
    return {
        "proposal_id": proposal_id,
        "source_memory_id": LEGACY_MEMORY_ID,
        "source_content_hash": source_hash,
        "target_memory_id": memory_id,
        "title": title,
        "created_at": created_at,
        "target_relative_path": memory_relative_path(memory_id),
        "home_path": home_path,
        "home_markdown": home,
        "files": tuple(files),
    }


class MarkdownMemoryStore:
    """Persist reports, evidence, and sources as one idempotent Markdown bundle."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        root_key = os.path.normcase(str(self.root))
        with _VAULT_LOCKS_GUARD:
            self._lock = _VAULT_LOCKS.setdefault(root_key, threading.RLock())

    def _resolve(self, relative_path: str) -> Path:
        target = (self.root / relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("memory path escapes the configured root")
        return target

    def _write_atomic(self, relative_path: str, content: str) -> None:
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(content)
                temp_path = handle.name
            os.replace(temp_path, target)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _load_frontmatter(markdown: str) -> dict[str, object]:
        _, _, frontmatter = _frontmatter_parts(markdown, label="Memory Home.md")
        return frontmatter

    @staticmethod
    def update_memory_home_with_note(
        home_markdown: str,
        wikilink: str,
        updated_at: str,
    ) -> str:
        return update_memory_home_with_note(home_markdown, wikilink, updated_at)

    @staticmethod
    def _file_snapshot(path: Path) -> tuple[bytes, str]:
        with path.open("rb") as handle:
            content = handle.read()
        return content, hashlib.sha256(content).hexdigest()

    def memory_home_snapshot(self, memory_id: str) -> tuple[str, str, str]:
        """Return canonical Home path, exact Markdown, and its SHA-256 snapshot."""
        with self._lock:
            self.get_memory(memory_id)
            home_path = f"{memory_relative_path(memory_id)}Home.md"
            home = resolve_vault_markdown_path(self.root, home_path)
            content, content_hash = self._file_snapshot(home)
        try:
            markdown = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Memory Home.md must be UTF-8 Markdown") from exc
        return home_path, markdown, content_hash

    @staticmethod
    def update_memory_home_with_import(
        home_markdown: str,
        import_wikilink: str,
        note_wikilink: str,
        updated_at: str,
    ) -> str:
        return update_memory_home_with_import(
            home_markdown,
            import_wikilink,
            note_wikilink,
            updated_at,
        )

    def _memory_directory(self, memory_id: str, name: str) -> Path:
        self.get_memory(memory_id)
        relative_memory = memory_relative_path(memory_id).rstrip("/")
        relative_directory = f"{memory_relative_path(memory_id)}{name}"
        self._reject_linked_vault_path(relative_directory)
        memory_root = self._resolve(relative_memory)
        directory = self._resolve(relative_directory)
        if not directory.is_relative_to(memory_root):
            raise ValueError(f"Memory {name} directory escapes the selected Memory")
        if not directory.is_dir():
            raise ValueError(f"Memory {name} directory does not exist")
        return directory

    def _reject_linked_vault_path(self, relative_path: str) -> None:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Memory path must be canonical and Vault-relative")
        current = self.root
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        for component in relative.parts:
            current = current / component
            try:
                file_attributes = getattr(os.lstat(current), "st_file_attributes", 0)
            except OSError:
                file_attributes = 0
            if (
                current.is_symlink()
                or is_junction(current)
                or bool(file_attributes & 0x400)
            ):
                raise ValueError("Memory path cannot traverse a symlink or junction")

    def _resolve_memory_markdown(
        self,
        memory_id: str,
        relative_path: str,
    ) -> Path:
        self._reject_linked_vault_path(relative_path)
        target = resolve_vault_markdown_path(self.root, relative_path)
        memory_root = self._resolve(
            memory_relative_path(memory_id).rstrip("/")
        )
        if not target.is_relative_to(memory_root):
            raise ValueError("Markdown path escapes the selected Memory")
        return target

    @staticmethod
    def _valid_import_frontmatter(
        markdown: str,
        *,
        memory_id: str,
    ) -> dict[str, object] | None:
        try:
            _, _, frontmatter = _frontmatter_parts(
                markdown,
                label="Memory import",
            )
        except (TypeError, ValueError):
            return None
        if (
            set(frontmatter) != _IMPORT_FRONTMATTER_FIELDS
            or frontmatter["type"] != "import"
            or frontmatter["memory_id"] != memory_id
            or frontmatter["origin"] != "import"
            or frontmatter["status"] != "confirmed"
            or frontmatter["tags"] != ["paperpilot"]
            or frontmatter["created_at"] != frontmatter["updated_at"]
            or not isinstance(frontmatter["source_kind"], str)
            or frontmatter["source_kind"] not in {"file", "text", "url"}
            or not isinstance(frontmatter["source_ref"], str)
            or not frontmatter["source_ref"]
            or frontmatter["source_ref"] != frontmatter["source_ref"].strip()
            or not isinstance(frontmatter["locator"], str)
            or not frontmatter["locator"]
            or frontmatter["locator"] != frontmatter["locator"].strip()
            or not isinstance(frontmatter["media_type"], str)
            or frontmatter["media_type"] not in _IMPORT_MEDIA_EXTENSIONS
            or not isinstance(frontmatter["byte_size"], int)
            or isinstance(frontmatter["byte_size"], bool)
            or frontmatter["byte_size"] < 1
            or not isinstance(frontmatter["content_hash"], str)
            or not _SHA256.fullmatch(frontmatter["content_hash"])
            or not isinstance(frontmatter["attachment_path"], str)
        ):
            return None
        try:
            validate_memory_attachment_path(
                frontmatter["attachment_path"],
                memory_id=memory_id,
            )
        except ValueError:
            return None
        return frontmatter

    def _find_import_note_path(
        self,
        memory_id: str,
        import_path: str,
    ) -> str | None:
        notes = self._memory_directory(memory_id, "notes")
        import_target = import_path[:-3]
        import_id = PurePosixPath(import_path).stem
        expected_note_id = f"Note-import-{import_id.removeprefix('Import-')}"
        matches: list[str] = []
        for candidate in sorted(notes.glob("*.md"), key=lambda path: path.name):
            relative_path = (
                f"{memory_relative_path(memory_id)}notes/{candidate.name}"
            )
            try:
                resolved = self._resolve_memory_markdown(memory_id, relative_path)
                markdown = resolved.read_text(encoding="utf-8")
                _, _, frontmatter = _frontmatter_parts(
                    markdown,
                    label="Memory import note",
                )
                if (
                    set(frontmatter) != _IMPORT_NOTE_FRONTMATTER_FIELDS
                    or not isinstance(frontmatter["id"], str)
                    or not _IMPORT_NOTE_ID.fullmatch(frontmatter["id"])
                    or frontmatter["id"] != expected_note_id
                    or candidate.stem != frontmatter["id"]
                    or frontmatter["type"] != "note"
                    or frontmatter["memory_id"] != memory_id
                    or frontmatter["origin"] != "import"
                    or frontmatter["status"] != "confirmed"
                    or frontmatter["tags"] != ["paperpilot"]
                    or frontmatter["created_at"] != frontmatter["updated_at"]
                ):
                    continue
                targets = {
                    _parse_wikilink(match.group(0))[0]
                    for match in _WIKILINK.finditer(markdown)
                }
            except (OSError, UnicodeError, TypeError, ValueError):
                continue
            if import_target in targets:
                matches.append(relative_path)
        if len(matches) > 1:
            raise MemoryWriteConflictError(
                "multiple import notes refer to the same Memory import"
            )
        return matches[0] if matches else None

    def _home_marks_completed_import(
        self,
        memory_id: str,
        import_path: str,
    ) -> bool:
        home_path = f"{memory_relative_path(memory_id)}Home.md"
        try:
            home = self._resolve_memory_markdown(memory_id, home_path)
            markdown = home.read_text(encoding="utf-8")
            lines, closing, frontmatter = _frontmatter_parts(
                markdown,
                label="Memory Home.md",
            )
        except (OSError, UnicodeError, TypeError, ValueError):
            return False
        if frontmatter["type"] != "home" or frontmatter["memory_id"] != memory_id:
            return False
        canonical_link = build_wikilink(import_path)
        if tuple(match.group(0) for match in _WIKILINK.finditer(markdown)).count(
            canonical_link
        ) != 1:
            return False
        headings = [
            index
            for index, line in enumerate(lines[closing + 1 :], start=closing + 1)
            if _HOME_IMPORTS_HEADING.fullmatch(line.rstrip("\r\n"))
        ]
        if len(headings) != 1:
            return False
        section_end = len(lines)
        for index in range(headings[0] + 1, len(lines)):
            if _HOME_SECTION_HEADING.match(lines[index].rstrip("\r\n")):
                section_end = index
                break
        section = "".join(lines[headings[0] + 1 : section_end])
        return canonical_link in tuple(
            match.group(0) for match in _WIKILINK.finditer(section)
        )

    def find_memory_import(
        self,
        memory_id: str,
        source_ref: str,
        locator: str,
        content_hash: str,
    ) -> MemoryImportDuplicate | None:
        """Find one exact existing import without maintaining an index."""
        validate_memory_id(memory_id)
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("source_ref must be a non-empty string")
        if not isinstance(locator, str):
            raise ValueError("locator must be a string")
        if not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256")

        with self._lock:
            imports = self._memory_directory(memory_id, "imports")
            exact: list[tuple[str, dict[str, object]]] = []
            for candidate in sorted(imports.glob("*.md"), key=lambda path: path.name):
                relative_path = (
                    f"{memory_relative_path(memory_id)}imports/{candidate.name}"
                )
                try:
                    resolved = self._resolve_memory_markdown(memory_id, relative_path)
                    markdown = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeError, ValueError):
                    continue
                frontmatter = self._valid_import_frontmatter(
                    markdown,
                    memory_id=memory_id,
                )
                if frontmatter is None:
                    continue
                if (
                    not isinstance(frontmatter["id"], str)
                    or not _IMPORT_ID.fullmatch(frontmatter["id"])
                    or candidate.stem != frontmatter["id"]
                ):
                    continue
                expected_fingerprint = _import_fingerprint(
                    str(frontmatter["source_ref"]),
                    str(frontmatter["locator"]),
                    str(frontmatter["content_hash"]),
                )
                if frontmatter["id"] != f"Import-{expected_fingerprint}":
                    continue
                expected_extension = _IMPORT_MEDIA_EXTENSIONS[
                    str(frontmatter["media_type"])
                ]
                expected_attachment = (
                    f"{memory_relative_path(memory_id)}attachments/"
                    f"Asset-{frontmatter['content_hash']}.{expected_extension}"
                )
                if frontmatter["attachment_path"] != expected_attachment:
                    continue
                exact_key = (
                    frontmatter["source_ref"] == source_ref
                    and frontmatter["locator"] == locator
                    and frontmatter["content_hash"] == content_hash
                )
                if exact_key:
                    exact.append((relative_path, frontmatter))
            if len(exact) > 1:
                raise MemoryWriteConflictError(
                    "multiple imports match the same source, locator, and content"
                )
            if not exact:
                return None

            import_path, frontmatter = exact[0]
            attachment_path = str(frontmatter["attachment_path"])
            attachment = resolve_memory_attachment_path(
                self.root,
                attachment_path,
                memory_id=memory_id,
            )
            if not attachment.is_file():
                raise MemoryWriteConflictError(
                    "existing import attachment is missing"
                )
            attachment_bytes, attachment_hash = self._file_snapshot(attachment)
            if (
                attachment_hash != content_hash
                or len(attachment_bytes) != frontmatter["byte_size"]
            ):
                raise MemoryWriteConflictError(
                    "existing import attachment does not match its content metadata"
                )
            if not self._home_marks_completed_import(memory_id, import_path):
                raise MemoryWriteConflictError(
                    "existing import has not reached its Home linearization point"
                )
            note_path = self._find_import_note_path(memory_id, import_path)
            wikilinks = [
                build_wikilink(import_path),
                build_attachment_wikilink(attachment_path),
            ]
            if note_path is not None:
                wikilinks.append(build_wikilink(note_path))
            return MemoryImportDuplicate(
                memory_id=memory_id,
                import_id=str(frontmatter["id"]),
                source_kind=str(frontmatter["source_kind"]),
                source_ref=source_ref,
                locator=locator,
                content_hash=content_hash,
                attachment_path=attachment_path,
                import_path=import_path,
                note_path=note_path,
                wikilinks=tuple(wikilinks),
            )

    @staticmethod
    def _proposal_strings(proposal: MemoryNoteProposal) -> None:
        for field_name in (
            "proposal_id",
            "answer_id",
            "memory_id",
            "note_id",
            "title",
            "target_path",
            "markdown",
            "wikilink",
            "home_path",
            "home_content_hash",
            "home_markdown",
        ):
            value = getattr(proposal, field_name, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"proposal {field_name} must be a non-empty string")

    def _validate_note_proposal_structure(
        self,
        proposal: MemoryNoteProposal,
    ) -> tuple[Path, Path, str]:
        if not isinstance(proposal, MemoryNoteProposal):
            raise TypeError("proposal must be a MemoryNoteProposal")
        self._proposal_strings(proposal)
        validate_memory_id(proposal.memory_id)
        if proposal.memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is read-only and cannot accept note proposals")
        self.get_memory(proposal.memory_id)

        expected_home = f"{memory_relative_path(proposal.memory_id)}Home.md"
        expected_target = (
            f"{memory_relative_path(proposal.memory_id)}"
            f"notes/{proposal.note_id}.md"
        )
        if proposal.home_path != expected_home:
            raise ValueError(f"proposal home_path must be {expected_home!r}")
        if proposal.target_path != expected_target:
            raise ValueError(f"proposal target_path must be {expected_target!r}")
        home = resolve_vault_markdown_path(self.root, proposal.home_path)
        target = resolve_vault_markdown_path(self.root, proposal.target_path)
        if not target.parent.is_dir():
            raise ValueError("Memory notes directory does not exist")
        if proposal.target_content_hash is not None:
            raise ValueError("new Memory note proposal target_content_hash must be None")
        if not _SHA256.fullmatch(proposal.home_content_hash):
            raise ValueError("proposal home_content_hash must be a lowercase SHA-256")

        _, _, frontmatter = _frontmatter_parts(
            proposal.markdown,
            label="Memory note proposal",
        )
        expected_properties = {
            "id": proposal.note_id,
            "type": "note",
            "memory_id": proposal.memory_id,
            "title": proposal.title,
            "origin": "conversation",
            "status": "confirmed",
        }
        expected_frontmatter_fields = {
            *expected_properties,
            "created_at",
            "updated_at",
            "tags",
        }
        if set(frontmatter) != expected_frontmatter_fields:
            raise ValueError(
                "Memory note proposal frontmatter must contain only fixed fields"
            )
        for field_name, expected_value in expected_properties.items():
            if frontmatter[field_name] != expected_value:
                raise ValueError(
                    f"Memory note proposal frontmatter {field_name} must match proposal"
                )
        if frontmatter["created_at"] != frontmatter["updated_at"]:
            raise ValueError(
                "Memory note proposal created_at and updated_at must match"
            )
        if frontmatter["tags"] != ["paperpilot"]:
            raise ValueError(
                "Memory note proposal tags must contain only 'paperpilot'"
            )

        note_target, _ = _parse_wikilink(proposal.wikilink)
        if note_target != proposal.target_path[:-3]:
            raise ValueError("proposal wikilink must target the proposed note")
        if not isinstance(proposal.source_paths, tuple):
            raise ValueError("proposal source_paths must be a tuple")
        if len(set(proposal.source_paths)) != len(proposal.source_paths):
            raise ValueError("proposal source_paths cannot contain duplicates")

        expected_source_targets: set[str] = set()
        memory_prefix = memory_relative_path(proposal.memory_id)
        for source_path in proposal.source_paths:
            if not isinstance(source_path, str) or not source_path.startswith(memory_prefix):
                raise ValueError("proposal sources must remain inside the selected Memory")
            source = resolve_vault_markdown_path(self.root, source_path)
            if not source.is_file():
                raise ValueError(f"proposal source does not exist: {source_path}")
            expected_source_targets.add(source_path[:-3])

        wikilink_free_markdown = _WIKILINK.sub("", proposal.markdown)
        if "[[" in wikilink_free_markdown or "]]" in wikilink_free_markdown:
            raise ValueError("Memory note proposal contains malformed WikiLink syntax")

        actual_source_targets: set[str] = set()
        for match in _WIKILINK.finditer(proposal.markdown):
            source_target, _ = _parse_wikilink(match.group(0))
            if not source_target.startswith(memory_prefix):
                raise ValueError("Memory note WikiLinks cannot cross Memory boundaries")
            actual_source_targets.add(source_target)
        if actual_source_targets != expected_source_targets:
            raise ValueError(
                "Memory note WikiLink targets must equal proposal source_paths"
            )
        return home, target, str(frontmatter["updated_at"])

    def validate_memory_note_proposal(self, proposal: MemoryNoteProposal) -> None:
        """Validate a proposal and its optimistic snapshots without writing files."""
        with self._lock:
            home, target, updated_at = self._validate_note_proposal_structure(proposal)
            current_content, current_hash = self._file_snapshot(home)
            if current_hash != proposal.home_content_hash:
                raise MemoryWriteConflictError(
                    "Memory Home.md changed after the proposal was prepared"
                )
            if target.exists():
                raise MemoryWriteConflictError(
                    "Memory note target already exists or was concurrently created"
                )
            current_markdown = current_content.decode("utf-8")
            expected_home_markdown = update_memory_home_with_note(
                current_markdown,
                proposal.wikilink,
                updated_at,
            )
            if proposal.home_markdown != expected_home_markdown:
                raise ValueError(
                    "proposal home_markdown must be the deterministic Home update"
                )

    @staticmethod
    def _import_proposal_strings(proposal: MemoryImportProposal) -> None:
        for field_name in (
            "proposal_id",
            "import_id",
            "note_id",
            "memory_id",
            "source_kind",
            "source_ref",
            "media_type",
            "content_hash",
            "attachment_path",
            "import_path",
            "import_markdown",
            "import_wikilink",
            "note_path",
            "note_markdown",
            "note_wikilink",
            "home_path",
            "home_content_hash",
            "home_markdown",
        ):
            value = getattr(proposal, field_name, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"proposal {field_name} must be a non-empty string")
        if not isinstance(proposal.locator, str):
            raise ValueError("proposal locator must be a string")

    def _validate_import_proposal_structure(
        self,
        proposal: MemoryImportProposal,
    ) -> tuple[Path, Path, Path, Path, str]:
        if not isinstance(proposal, MemoryImportProposal):
            raise TypeError("proposal must be a MemoryImportProposal")
        self._import_proposal_strings(proposal)
        validate_memory_id(proposal.memory_id)
        if proposal.memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is read-only and cannot accept imports")
        self.get_memory(proposal.memory_id)
        if proposal.source_kind not in {"file", "text", "url"}:
            raise ValueError("proposal source_kind must be file, text, or url")
        if proposal.source_ref != proposal.source_ref.strip():
            raise ValueError("proposal source_ref must use canonical surrounding whitespace")
        if not proposal.locator.strip() or proposal.locator != proposal.locator.strip():
            raise ValueError("proposal locator must be a canonical non-empty string")
        if not _IMPORT_ID.fullmatch(proposal.import_id):
            raise ValueError("proposal import_id must be a canonical Import-* ID")
        if not _IMPORT_NOTE_ID.fullmatch(proposal.note_id):
            raise ValueError("proposal note_id must be a canonical Note-* ID")
        if not _SHA256.fullmatch(proposal.content_hash):
            raise ValueError("proposal content_hash must be a lowercase SHA-256")
        if not _SHA256.fullmatch(proposal.home_content_hash):
            raise ValueError("proposal home_content_hash must be a lowercase SHA-256")
        if not isinstance(proposal.attachment_bytes, bytes):
            raise ValueError("proposal attachment_bytes must be bytes")
        if (
            not isinstance(proposal.byte_size, int)
            or isinstance(proposal.byte_size, bool)
            or proposal.byte_size < 1
            or proposal.byte_size != len(proposal.attachment_bytes)
        ):
            raise ValueError("proposal byte_size must equal attachment byte length")
        if hashlib.sha256(proposal.attachment_bytes).hexdigest() != proposal.content_hash:
            raise ValueError("proposal content_hash must match attachment bytes")
        fingerprint = _import_fingerprint(
            proposal.source_ref,
            proposal.locator,
            proposal.content_hash,
        )
        if proposal.import_id != f"Import-{fingerprint}":
            raise ValueError(
                "proposal import_id must be derived from source_ref, locator, and content_hash"
            )
        if proposal.note_id != f"Note-import-{fingerprint}":
            raise ValueError(
                "proposal note_id must be derived from source_ref, locator, and content_hash"
            )
        extension = _IMPORT_MEDIA_EXTENSIONS.get(proposal.media_type)
        if extension is None:
            raise ValueError("proposal media_type must be PDF, plain text, or HTML")

        prefix = memory_relative_path(proposal.memory_id)
        expected_attachment_path = (
            f"{prefix}attachments/Asset-{proposal.content_hash}.{extension}"
        )
        expected_import_path = f"{prefix}imports/{proposal.import_id}.md"
        expected_note_path = f"{prefix}notes/{proposal.note_id}.md"
        expected_home_path = f"{prefix}Home.md"
        expected_paths = {
            "attachment_path": expected_attachment_path,
            "import_path": expected_import_path,
            "note_path": expected_note_path,
            "home_path": expected_home_path,
        }
        for field_name, expected in expected_paths.items():
            if getattr(proposal, field_name) != expected:
                raise ValueError(f"proposal {field_name} must be {expected!r}")

        attachment = resolve_memory_attachment_path(
            self.root,
            proposal.attachment_path,
            memory_id=proposal.memory_id,
        )
        import_target = self._resolve_memory_markdown(
            proposal.memory_id,
            proposal.import_path,
        )
        note_target = self._resolve_memory_markdown(
            proposal.memory_id,
            proposal.note_path,
        )
        home = self._resolve_memory_markdown(
            proposal.memory_id,
            proposal.home_path,
        )
        for directory_name, target in (
            ("attachments", attachment),
            ("imports", import_target),
            ("notes", note_target),
        ):
            expected_directory = self._memory_directory(
                proposal.memory_id,
                directory_name,
            )
            if target.parent != expected_directory:
                raise ValueError(
                    f"proposal {directory_name} target escapes its canonical directory"
                )

        _, _, import_frontmatter = _frontmatter_parts(
            proposal.import_markdown,
            label="Memory import proposal",
        )
        if set(import_frontmatter) != _IMPORT_FRONTMATTER_FIELDS:
            raise ValueError(
                "Memory import proposal frontmatter must contain only fixed fields"
            )
        expected_import_values: dict[str, object] = {
            "id": proposal.import_id,
            "type": "import",
            "memory_id": proposal.memory_id,
            "origin": "import",
            "status": "confirmed",
            "tags": ["paperpilot"],
            "source_kind": proposal.source_kind,
            "source_ref": proposal.source_ref,
            "locator": proposal.locator,
            "media_type": proposal.media_type,
            "byte_size": proposal.byte_size,
            "content_hash": proposal.content_hash,
            "attachment_path": proposal.attachment_path,
        }
        for field_name, expected in expected_import_values.items():
            if import_frontmatter[field_name] != expected:
                raise ValueError(
                    f"Memory import frontmatter {field_name} must match proposal"
                )
        if import_frontmatter["created_at"] != import_frontmatter["updated_at"]:
            raise ValueError(
                "Memory import created_at and updated_at must match"
            )
        import_link_target, _ = _parse_wikilink(proposal.import_wikilink)
        if import_link_target != proposal.import_path[:-3]:
            raise ValueError("proposal import_wikilink must target the import note")

        import_without_links = _WIKILINK.sub("", proposal.import_markdown)
        if "[[" in import_without_links or "]]" in import_without_links:
            raise ValueError("Memory import contains malformed WikiLink syntax")
        attachment_targets: set[str] = set()
        for match in _WIKILINK.finditer(proposal.import_markdown):
            target, _ = _parse_attachment_wikilink(match.group(0))
            attachment_targets.add(target)
        if attachment_targets != {proposal.attachment_path}:
            raise ValueError(
                "Memory import must link exactly its content-addressed attachment"
            )

        _, _, note_frontmatter = _frontmatter_parts(
            proposal.note_markdown,
            label="Memory import note proposal",
        )
        if set(note_frontmatter) != _IMPORT_NOTE_FRONTMATTER_FIELDS:
            raise ValueError(
                "Memory import note frontmatter must contain only fixed fields"
            )
        expected_note_values: dict[str, object] = {
            "id": proposal.note_id,
            "type": "note",
            "memory_id": proposal.memory_id,
            "origin": "import",
            "status": "confirmed",
            "tags": ["paperpilot"],
        }
        for field_name, expected in expected_note_values.items():
            if note_frontmatter[field_name] != expected:
                raise ValueError(
                    f"Memory import note frontmatter {field_name} must match proposal"
                )
        if note_frontmatter["created_at"] != note_frontmatter["updated_at"]:
            raise ValueError(
                "Memory import note created_at and updated_at must match"
            )
        note_link_target, _ = _parse_wikilink(proposal.note_wikilink)
        if note_link_target != proposal.note_path[:-3]:
            raise ValueError("proposal note_wikilink must target the import note")

        if not isinstance(proposal.note_source_paths, tuple):
            raise ValueError("proposal note_source_paths must be a tuple")
        if (
            not proposal.note_source_paths
            or proposal.import_path not in proposal.note_source_paths
            or len(set(proposal.note_source_paths)) != len(proposal.note_source_paths)
        ):
            raise ValueError(
                "proposal note_source_paths must be unique and include import_path"
            )
        source_targets: set[str] = set()
        for source_path in proposal.note_source_paths:
            if not isinstance(source_path, str) or not source_path.startswith(prefix):
                raise ValueError("import note sources must stay inside the selected Memory")
            source = self._resolve_memory_markdown(proposal.memory_id, source_path)
            if source_path != proposal.import_path and not source.is_file():
                raise ValueError(f"import note source does not exist: {source_path}")
            source_targets.add(source_path[:-3])
        note_without_links = _WIKILINK.sub("", proposal.note_markdown)
        if "[[" in note_without_links or "]]" in note_without_links:
            raise ValueError("Memory import note contains malformed WikiLink syntax")
        note_targets: set[str] = set()
        for match in _WIKILINK.finditer(proposal.note_markdown):
            target, _ = _parse_wikilink(match.group(0))
            if not target.startswith(prefix):
                raise ValueError("Memory import note WikiLinks cannot cross Memories")
            note_targets.add(target)
        if note_targets != source_targets:
            raise ValueError(
                "Memory import note WikiLinks must equal note_source_paths"
            )
        return (
            home,
            attachment,
            import_target,
            note_target,
            str(import_frontmatter["updated_at"]),
        )

    def _validate_import_proposal_state(
        self,
        proposal: MemoryImportProposal,
        home: Path,
        attachment: Path,
        import_target: Path,
        note_target: Path,
        updated_at: str,
    ) -> None:
        current_home, current_home_hash = self._file_snapshot(home)
        if current_home_hash != proposal.home_content_hash:
            raise MemoryWriteConflictError(
                "Memory Home.md changed after the import proposal was prepared"
            )
        for label, target in (("import", import_target), ("note", note_target)):
            if target.exists():
                raise MemoryWriteConflictError(
                    f"Memory {label} target already exists or was concurrently created"
                )
        if attachment.exists():
            if not attachment.is_file():
                raise MemoryWriteConflictError(
                    "Memory attachment target exists but is not a file"
                )
            _, existing_hash = self._file_snapshot(attachment)
            if existing_hash != proposal.content_hash:
                raise MemoryWriteConflictError(
                    "Memory attachment path already contains different content"
                )
        try:
            current_home_markdown = current_home.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Memory Home.md must be UTF-8 Markdown") from exc
        expected_home = update_memory_home_with_import(
            current_home_markdown,
            proposal.import_wikilink,
            proposal.note_wikilink,
            updated_at,
        )
        if proposal.home_markdown != expected_home:
            raise ValueError(
                "proposal home_markdown must be the deterministic import Home update"
            )

    def validate_memory_import_proposal(
        self,
        proposal: MemoryImportProposal,
    ) -> None:
        """Strictly validate an import proposal without writing any file."""
        with self._lock:
            parts = self._validate_import_proposal_structure(proposal)
            self._validate_import_proposal_state(proposal, *parts)

    @staticmethod
    def _write_commit_temp(
        parent: Path,
        name: str,
        content: str | bytes,
    ) -> Path:
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=parent,
                prefix=f".{name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(content.encode("utf-8") if isinstance(content, str) else content)
                temp_path = handle.name
            return Path(temp_path)
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def _replace_home_if_snapshot_matches(
        self,
        home: Path,
        replacement: Path,
        expected_hash: str,
        replacement_hash: str,
    ) -> None:
        try:
            preserved = _atomic_replace_preserving_old(home, replacement)
        except Exception:
            if replacement.exists():
                _discard_preserved(replacement)
            raise
        _, replaced_hash = self._file_snapshot(preserved)
        if replaced_hash == expected_hash:
            _discard_preserved(preserved)
            return

        desired = preserved
        desired_hash = replaced_hash
        expected_current_hash = replacement_hash
        for _ in range(_MAX_HOME_RESTORE_EXCHANGES):
            displaced = _atomic_replace_preserving_old(home, desired)
            _, displaced_hash = self._file_snapshot(displaced)
            if displaced_hash == expected_current_hash:
                _discard_preserved(displaced)
                raise MemoryWriteConflictError(
                    "Memory Home.md changed at the atomic replacement point"
                )

            # Another external edit reached canonical Home before this restore.
            # That displaced version becomes the next desired canonical version;
            # nothing unknown is deleted while the state machine converges.
            expected_current_hash = desired_hash
            desired = displaced
            desired_hash = displaced_hash

        recovery_path = desired.relative_to(self.root).as_posix()
        raise MemoryWriteConflictError(
            "Memory Home.md kept changing during rollback; latest content is "
            f"preserved at {recovery_path}"
        )

    def commit_memory_note(self, proposal: MemoryNoteProposal) -> dict[str, str]:
        """Atomically create one proposed note and replace its validated Home."""
        with self._lock:
            self.validate_memory_note_proposal(proposal)
            home = resolve_vault_markdown_path(self.root, proposal.home_path)
            target = resolve_vault_markdown_path(self.root, proposal.target_path)
            note_temp: Path | None = None
            home_temp: Path | None = None
            note_created = False
            created_identity: tuple[int, int] | None = None
            try:
                note_temp = self._write_commit_temp(
                    target.parent,
                    target.name,
                    proposal.markdown,
                )
                home_temp = self._write_commit_temp(
                    home.parent,
                    home.name,
                    proposal.home_markdown,
                )
                try:
                    os.link(note_temp, target)
                except OSError as exc:
                    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST:
                        raise MemoryWriteConflictError(
                            "Memory note target was concurrently created"
                        ) from None
                    raise
                note_created = True
                created_stat = target.stat()
                created_identity = (created_stat.st_dev, created_stat.st_ino)
                note_temp.unlink()
                note_temp = None

                _, current_home_hash = self._file_snapshot(home)
                if current_home_hash != proposal.home_content_hash:
                    raise MemoryWriteConflictError(
                        "Memory Home.md changed during note commit"
                    )
                replacement_hash = hashlib.sha256(
                    proposal.home_markdown.encode("utf-8")
                ).hexdigest()
                replacement = home_temp
                home_temp = None
                self._replace_home_if_snapshot_matches(
                    home,
                    replacement,
                    proposal.home_content_hash,
                    replacement_hash,
                )
            except Exception:
                if note_created and target.exists():
                    target_stat = target.stat()
                    target_identity = (target_stat.st_dev, target_stat.st_ino)
                    if target_identity == created_identity:
                        target.unlink()
                raise
            finally:
                for temp_path in (note_temp, home_temp):
                    if temp_path is not None and temp_path.exists():
                        temp_path.unlink()

        return {
            "memory_id": proposal.memory_id,
            "target_path": proposal.target_path,
            "home_path": proposal.home_path,
            "wikilink": proposal.wikilink,
        }

    @staticmethod
    def _created_file_identity(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino

    @classmethod
    def _rollback_created_file(
        cls,
        path: Path,
        identity: tuple[int, int],
    ) -> None:
        try:
            if path.exists() and cls._created_file_identity(path) == identity:
                path.unlink()
        except FileNotFoundError:
            return

    def _attachment_has_linearized_import(
        self,
        memory_id: str,
        attachment_path: str,
    ) -> bool:
        """Return whether a completed managed import currently owns an attachment."""
        imports = self._memory_directory(memory_id, "imports")
        for candidate in sorted(imports.glob("*.md"), key=lambda path: path.name):
            relative_path = f"{memory_relative_path(memory_id)}imports/{candidate.name}"
            try:
                resolved = self._resolve_memory_markdown(memory_id, relative_path)
                frontmatter = self._valid_import_frontmatter(
                    resolved.read_text(encoding="utf-8"),
                    memory_id=memory_id,
                )
                if frontmatter is None:
                    continue
                fingerprint = _import_fingerprint(
                    str(frontmatter["source_ref"]),
                    str(frontmatter["locator"]),
                    str(frontmatter["content_hash"]),
                )
                expected_id = f"Import-{fingerprint}"
                if (
                    candidate.stem != expected_id
                    or frontmatter["id"] != expected_id
                    or frontmatter["attachment_path"] != attachment_path
                ):
                    continue
                extension = _IMPORT_MEDIA_EXTENSIONS[str(frontmatter["media_type"])]
                expected_attachment = (
                    f"{memory_relative_path(memory_id)}attachments/"
                    f"Asset-{frontmatter['content_hash']}.{extension}"
                )
                if attachment_path != expected_attachment:
                    continue
                if self._home_marks_completed_import(memory_id, relative_path):
                    return True
            except (OSError, UnicodeError, TypeError, ValueError):
                continue
        return False

    def _rollback_import_created_files(
        self,
        created: list[tuple[Path, tuple[int, int]]],
        *,
        memory_id: str,
        attachment: Path,
        attachment_path: str,
    ) -> None:
        preserve_attachment = False
        if any(path == attachment for path, _ in created):
            try:
                preserve_attachment = self._attachment_has_linearized_import(
                    memory_id,
                    attachment_path,
                )
            except (OSError, RuntimeError, ValueError):
                # Ambiguous concurrent external state must not be deleted. In the
                # ordinary failure path the scan succeeds and unreferenced files
                # are still removed, preserving the zero-half-file guarantee.
                preserve_attachment = True
        for path, identity in reversed(created):
            if preserve_attachment and path == attachment:
                continue
            self._rollback_created_file(path, identity)

    @staticmethod
    def _import_result(
        proposal: MemoryImportProposal,
        *,
        status: str,
        duplicate: MemoryImportDuplicate | None = None,
    ) -> dict[str, object]:
        if duplicate is None:
            attachment_path = proposal.attachment_path
            import_path = proposal.import_path
            note_path = proposal.note_path
            wikilinks = (
                proposal.import_wikilink,
                build_attachment_wikilink(proposal.attachment_path),
                proposal.note_wikilink,
            )
        else:
            attachment_path = duplicate.attachment_path
            import_path = duplicate.import_path
            note_path = duplicate.note_path
            wikilinks = duplicate.wikilinks
        return {
            "status": status,
            "memory_id": proposal.memory_id,
            "attachment_path": attachment_path,
            "import_path": import_path,
            "note_path": note_path,
            "home_path": proposal.home_path,
            "wikilinks": wikilinks,
        }

    def commit_memory_import(
        self,
        proposal: MemoryImportProposal,
    ) -> dict[str, object]:
        """Commit one attachment/import/note batch with Home as linearization point."""
        with self._lock:
            parts = self._validate_import_proposal_structure(proposal)
            duplicate = self.find_memory_import(
                proposal.memory_id,
                proposal.source_ref,
                proposal.locator,
                proposal.content_hash,
            )
            if duplicate is not None:
                return self._import_result(
                    proposal,
                    status="duplicate",
                    duplicate=duplicate,
                )
            self._validate_import_proposal_state(proposal, *parts)
            home, attachment, import_target, note_target, _ = parts

            attachment_temp: Path | None = None
            import_temp: Path | None = None
            note_temp: Path | None = None
            home_temp: Path | None = None
            created: list[tuple[Path, tuple[int, int]]] = []
            try:
                attachment_temp = self._write_commit_temp(
                    attachment.parent,
                    attachment.name,
                    proposal.attachment_bytes,
                )
                import_temp = self._write_commit_temp(
                    import_target.parent,
                    import_target.name,
                    proposal.import_markdown,
                )
                note_temp = self._write_commit_temp(
                    note_target.parent,
                    note_target.name,
                    proposal.note_markdown,
                )
                home_temp = self._write_commit_temp(
                    home.parent,
                    home.name,
                    proposal.home_markdown,
                )

                try:
                    os.link(attachment_temp, attachment)
                except OSError as exc:
                    if not (isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST):
                        raise
                    if not attachment.is_file():
                        raise MemoryWriteConflictError(
                            "Memory attachment target was concurrently created"
                        ) from None
                    _, existing_hash = self._file_snapshot(attachment)
                    if existing_hash != proposal.content_hash:
                        raise MemoryWriteConflictError(
                            "Memory attachment target was concurrently changed"
                        ) from None
                else:
                    created.append(
                        (attachment, self._created_file_identity(attachment))
                    )
                attachment_temp.unlink()
                attachment_temp = None

                try:
                    os.link(import_temp, import_target)
                except OSError as exc:
                    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST:
                        duplicate = self.find_memory_import(
                            proposal.memory_id,
                            proposal.source_ref,
                            proposal.locator,
                            proposal.content_hash,
                        )
                        if duplicate is not None:
                            self._rollback_import_created_files(
                                created,
                                memory_id=proposal.memory_id,
                                attachment=attachment,
                                attachment_path=proposal.attachment_path,
                            )
                            created.clear()
                            return self._import_result(
                                proposal,
                                status="duplicate",
                                duplicate=duplicate,
                            )
                        raise MemoryWriteConflictError(
                            "Memory import target was concurrently created"
                        ) from None
                    raise
                created.append(
                    (import_target, self._created_file_identity(import_target))
                )
                import_temp.unlink()
                import_temp = None

                try:
                    os.link(note_temp, note_target)
                except OSError as exc:
                    if isinstance(exc, FileExistsError) or exc.errno == errno.EEXIST:
                        raise MemoryWriteConflictError(
                            "Memory import note target was concurrently created"
                        ) from None
                    raise
                created.append((note_target, self._created_file_identity(note_target)))
                note_temp.unlink()
                note_temp = None

                _, current_home_hash = self._file_snapshot(home)
                if current_home_hash != proposal.home_content_hash:
                    raise MemoryWriteConflictError(
                        "Memory Home.md changed during import commit"
                    )
                replacement_hash = hashlib.sha256(
                    proposal.home_markdown.encode("utf-8")
                ).hexdigest()
                replacement = home_temp
                home_temp = None
                self._replace_home_if_snapshot_matches(
                    home,
                    replacement,
                    proposal.home_content_hash,
                    replacement_hash,
                )
                created.clear()
            except Exception:
                self._rollback_import_created_files(
                    created,
                    memory_id=proposal.memory_id,
                    attachment=attachment,
                    attachment_path=proposal.attachment_path,
                )
                raise
            finally:
                for temp_path in (
                    attachment_temp,
                    import_temp,
                    note_temp,
                    home_temp,
                ):
                    if temp_path is not None and temp_path.exists():
                        temp_path.unlink()

        return self._import_result(proposal, status="committed")

    def _descriptor_from_home(self, memory_id: str) -> MemoryDescriptor:
        validate_memory_id(memory_id)
        relative_path = f"{memory_relative_path(memory_id)}Home.md"
        home = self._resolve(relative_path)
        if not home.is_file():
            raise FileNotFoundError(f"Memory does not exist: {memory_id}")
        frontmatter = self._load_frontmatter(home.read_text(encoding="utf-8"))
        if frontmatter["memory_id"] != memory_id or frontmatter["type"] != "home":
            raise ValueError(f"Memory Home.md identity does not match {memory_id}")
        return validate_memory_descriptor(
            MemoryDescriptor(
                memory_id=memory_id,
                title=str(frontmatter["title"]),
                relative_path=memory_relative_path(memory_id),
                created_at=str(frontmatter["created_at"]),
                updated_at=str(frontmatter["updated_at"]),
            )
        )

    def prepare_legacy_memory_migration(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> dict[str, object]:
        """Build a complete copy-on-publish migration preview without writes."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError("legacy migration title must be a non-empty string")
        target_memory_id = memory_id or f"M-legacy-{uuid.uuid4().hex}"
        validate_memory_id(target_memory_id)
        if target_memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is reserved for the read-only legacy Memory")

        with self._lock:
            self._reject_linked_vault_path("Memories")
            target = self._resolve(memory_relative_path(target_memory_id).rstrip("/"))
            if target.exists():
                raise FileExistsError(
                    f"Memory already exists: {target_memory_id}"
                )
            records, source_hash = _legacy_snapshot(self.root)
            proposal = _build_legacy_migration_proposal(
                records,
                source_hash,
                proposal_id=f"LegacyMigration-{uuid.uuid4().hex}",
                memory_id=target_memory_id,
                title=title.strip(),
                created_at=self._timestamp(),
            )
            self._validate_legacy_migration_documents(proposal)
            return proposal

    def _validate_legacy_migration_documents(
        self,
        proposal: Mapping[str, object],
        *,
        staging: Path | None = None,
    ) -> MemoryDescriptor:
        if not isinstance(proposal, Mapping):
            raise TypeError("legacy migration proposal must be a mapping")
        if set(proposal) != _LEGACY_MIGRATION_PROPOSAL_KEYS:
            raise ValueError("legacy migration proposal fields do not match the contract")
        for field_name in (
            "proposal_id",
            "source_memory_id",
            "source_content_hash",
            "target_memory_id",
            "title",
            "created_at",
            "target_relative_path",
            "home_path",
            "home_markdown",
        ):
            value = proposal[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"legacy migration proposal {field_name} must be non-empty"
                )
        if proposal["source_memory_id"] != LEGACY_MEMORY_ID:
            raise ValueError("legacy migration source identity must remain M-legacy")
        if not re.fullmatch(r"LegacyMigration-[0-9a-f]{32}", str(proposal["proposal_id"])):
            raise ValueError("legacy migration proposal_id is not canonical")
        if not _SHA256.fullmatch(str(proposal["source_content_hash"])):
            raise ValueError("legacy migration source hash must be a lowercase SHA-256")
        memory_id = validate_memory_id(str(proposal["target_memory_id"]))
        if memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is reserved for the read-only legacy Memory")
        expected_relative = memory_relative_path(memory_id)
        expected_home = f"{expected_relative}Home.md"
        if proposal["target_relative_path"] != expected_relative:
            raise ValueError("legacy migration target_relative_path is not canonical")
        if proposal["home_path"] != expected_home:
            raise ValueError("legacy migration home_path is not canonical")

        home_markdown = str(proposal["home_markdown"])
        _, _, home_frontmatter = _frontmatter_parts(
            home_markdown,
            label="migrated Memory Home.md",
        )
        if (
            home_frontmatter["type"] != "home"
            or home_frontmatter["memory_id"] != memory_id
            or home_frontmatter["title"] != proposal["title"]
            or home_frontmatter["created_at"] != proposal["created_at"]
            or home_frontmatter["updated_at"] != proposal["created_at"]
        ):
            raise ValueError("migrated Memory Home.md does not match its proposal")

        files = proposal["files"]
        if not isinstance(files, tuple) or not files:
            raise ValueError("legacy migration proposal files must be a non-empty tuple")
        source_paths: set[str] = set()
        target_paths: set[str] = {expected_home}
        file_by_target: dict[str, Mapping[str, str]] = {}
        for item in files:
            if not isinstance(item, Mapping) or set(item) != {
                "source_path",
                "source_content_hash",
                "target_path",
                "markdown",
                "wikilink",
            }:
                raise ValueError("legacy migration file entry is invalid")
            if any(not isinstance(value, str) for value in item.values()):
                raise ValueError("legacy migration file fields must be strings")
            source_path = str(item["source_path"])
            target_path = str(item["target_path"])
            if source_path in source_paths or target_path in target_paths:
                raise ValueError("legacy migration paths must be unique")
            source_paths.add(source_path)
            target_paths.add(target_path)
            if not _SHA256.fullmatch(str(item["source_content_hash"])):
                raise ValueError("legacy migration file hash must be a SHA-256")
            relative = PurePosixPath(target_path)
            if (
                len(relative.parts) != 4
                or relative.parts[0] != "Memories"
                or relative.parts[1] != memory_id
                or relative.parts[2] not in {"reports", "evidence", "sources"}
            ):
                raise ValueError("legacy migration target leaves the selected Memory")
            resolve_vault_markdown_path(self.root, target_path)
            if build_wikilink(target_path) != item["wikilink"]:
                raise ValueError("legacy migration file WikiLink is not canonical")
            markdown = str(item["markdown"])
            _, _, frontmatter = _frontmatter_parts(
                markdown,
                label=f"migrated Markdown {target_path}",
            )
            if (
                frontmatter["id"] != relative.stem
                or frontmatter["type"] != relative.parts[2].removesuffix("s")
                or frontmatter["memory_id"] != memory_id
                or frontmatter["origin"] != "research"
                or frontmatter["status"] != "confirmed"
                or frontmatter["tags"] != ["paperpilot"]
            ):
                raise ValueError(
                    f"migrated Markdown frontmatter does not match its path: {target_path}"
                )
            file_by_target[target_path] = item  # type: ignore[assignment]

        for relative_path, markdown in (
            (expected_home, home_markdown),
            *(
                (target_path, str(item["markdown"]))
                for target_path, item in file_by_target.items()
            ),
        ):
            for match in _WIKILINK.finditer(markdown):
                target, _ = _parse_wikilink(match.group(0))
                target_path = f"{target}.md"
                if target_path in target_paths:
                    continue
                external = resolve_vault_markdown_path(self.root, target_path)
                if not external.is_file():
                    raise ValueError(
                        f"migrated WikiLink target does not exist: {target_path}"
                    )
            unmatched = _WIKILINK.sub("", markdown)
            if "[[" in unmatched or "]]" in unmatched:
                raise ValueError(
                    f"migrated Markdown has malformed WikiLink syntax: {relative_path}"
                )

        report_links = {
            f"{match.group(1).split('|', 1)[0]}.md"
            for match in _WIKILINK.finditer(home_markdown)
        }
        expected_reports = {
            path
            for path in target_paths
            if PurePosixPath(path).parts[2:3] == ("reports",)
        }
        if not expected_reports.issubset(report_links):
            raise ValueError("migrated Memory Home.md does not link every report")

        descriptor = validate_memory_descriptor(
            MemoryDescriptor(
                memory_id=memory_id,
                title=str(proposal["title"]),
                relative_path=expected_relative,
                created_at=str(proposal["created_at"]),
                updated_at=str(proposal["created_at"]),
            )
        )

        if staging is not None:
            expected_files = {
                "Home.md",
                *(
                    PurePosixPath(path).relative_to(
                        "Memories", memory_id
                    ).as_posix()
                    for path in file_by_target
                ),
            }
            actual_files = {
                path.relative_to(staging).as_posix()
                for path in staging.rglob("*")
                if path.is_file()
            }
            if actual_files != expected_files:
                raise ValueError("legacy migration staging contains unexpected files")
            with (staging / "Home.md").open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                staged_home = handle.read()
            if staged_home != home_markdown:
                raise ValueError("legacy migration staging Home.md changed")
            for target_path, item in file_by_target.items():
                relative = PurePosixPath(target_path).relative_to(
                    "Memories", memory_id
                )
                with staging.joinpath(*relative.parts).open(
                    "r", encoding="utf-8", newline=""
                ) as handle:
                    staged_markdown = handle.read()
                if staged_markdown != item["markdown"]:
                    raise ValueError(
                        f"legacy migration staging file changed: {target_path}"
                    )
        return descriptor

    def commit_legacy_memory_migration(
        self,
        proposal: Mapping[str, object],
    ) -> MemoryDescriptor:
        """Publish a verified legacy copy with one same-volume directory rename."""
        with self._lock:
            descriptor = self._validate_legacy_migration_documents(proposal)
            records, source_hash = _legacy_snapshot(self.root)
            if source_hash != proposal["source_content_hash"]:
                raise MemoryWriteConflictError(
                    "legacy Memory changed after the migration preview"
                )
            expected = _build_legacy_migration_proposal(
                records,
                source_hash,
                proposal_id=str(proposal["proposal_id"]),
                memory_id=descriptor.memory_id,
                title=descriptor.title,
                created_at=descriptor.created_at,
            )
            if expected != dict(proposal):
                raise ValueError("legacy migration proposal content was modified")

            self._reject_linked_vault_path("Memories")
            memories_root = self._resolve("Memories")
            target = self._resolve(descriptor.relative_path.rstrip("/"))
            if target.exists():
                raise FileExistsError(
                    f"Memory already exists: {descriptor.memory_id}"
                )
            created_memories_root = not memories_root.exists()
            memories_root.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{descriptor.memory_id}.migration.",
                    dir=memories_root,
                )
            )
            try:
                for directory in _MEMORY_DIRECTORIES:
                    (staging / directory).mkdir()
                (staging / "Home.md").write_text(
                    str(proposal["home_markdown"]),
                    encoding="utf-8",
                    newline="\n",
                )
                files = proposal["files"]
                assert isinstance(files, tuple)
                for item in files:
                    assert isinstance(item, Mapping)
                    target_path = PurePosixPath(str(item["target_path"]))
                    relative = target_path.relative_to(
                        "Memories", descriptor.memory_id
                    )
                    destination = staging.joinpath(*relative.parts)
                    destination.write_text(
                        str(item["markdown"]),
                        encoding="utf-8",
                        newline="\n",
                    )

                self._validate_legacy_migration_documents(
                    proposal,
                    staging=staging,
                )
                _, final_source_hash = _legacy_snapshot(self.root)
                if final_source_hash != source_hash:
                    raise MemoryWriteConflictError(
                        "legacy Memory changed during migration commit"
                    )
                try:
                    staging.rename(target)
                except FileExistsError:
                    raise FileExistsError(
                        f"Memory already exists: {descriptor.memory_id}"
                    ) from None
                except OSError as exc:
                    if target.exists():
                        raise FileExistsError(
                            f"Memory already exists: {descriptor.memory_id}"
                        ) from None
                    raise exc
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
                if created_memories_root:
                    try:
                        memories_root.rmdir()
                    except OSError:
                        pass
            return descriptor

    def create_memory(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> MemoryDescriptor:
        """Atomically create one complete Memory directory in this Vault."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Memory title must be a non-empty string")
        if memory_id is None:
            memory_id = f"M-{uuid.uuid4().hex}"
        validate_memory_id(memory_id)
        if memory_id == LEGACY_MEMORY_ID:
            raise ValueError("M-legacy is reserved for the read-only legacy Memory")
        descriptor_path = memory_relative_path(memory_id)
        target = self._resolve(descriptor_path.rstrip("/"))
        memories_root = self._resolve("Memories")

        with self._lock:
            memories_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise FileExistsError(f"Memory already exists: {memory_id}")
            staging = Path(
                tempfile.mkdtemp(prefix=f".{memory_id}.", dir=memories_root)
            )
            timestamp = self._timestamp()
            try:
                for directory in _MEMORY_DIRECTORIES:
                    (staging / directory).mkdir()
                (staging / "Home.md").write_text(
                    render_memory_home(
                        memory_id=memory_id,
                        title=title.strip(),
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                try:
                    staging.rename(target)
                except FileExistsError:
                    raise FileExistsError(f"Memory already exists: {memory_id}") from None
                except OSError as exc:
                    if target.exists():
                        raise FileExistsError(
                            f"Memory already exists: {memory_id}"
                        ) from None
                    raise exc
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return self._descriptor_from_home(memory_id)

    def get_memory(self, memory_id: str) -> MemoryDescriptor:
        """Read the latest descriptor directly from a Memory's Home Markdown."""
        return self._descriptor_from_home(memory_id)

    def list_memories(self) -> tuple[MemoryDescriptor, ...]:
        """List complete Memories from Home Markdown without maintaining an index."""
        memories_root = self._resolve("Memories")
        if not memories_root.is_dir():
            return ()
        descriptors: list[MemoryDescriptor] = []
        for candidate in sorted(memories_root.iterdir(), key=lambda path: path.name):
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            try:
                validate_memory_id(candidate.name)
            except ValueError:
                continue
            descriptors.append(self._descriptor_from_home(candidate.name))
        return tuple(descriptors)

    def persist_research(
        self,
        brief: ResearchBrief,
        result: ResearchResult,
        identity: ExecutionIdentity,
        *,
        memory_id: str | None = None,
        report_body_markdown: str | None = None,
        report_architecture: str = "supervisor_v2",
    ) -> tuple[str, MemoryManifest]:
        """Atomically replace stable note paths; repeated calls create no duplicates."""
        identity.validate()
        if identity.depth != 0:
            raise ValueError("only the root Research Agent can persist a final report")

        if memory_id is not None:
            if memory_id == LEGACY_MEMORY_ID:
                raise ValueError("M-legacy is read-only and cannot accept research output")
            self.get_memory(memory_id)

        report_note = report_note_id(identity.root_thread_id)
        unique_evidence = list(
            {item.evidence_id: item for item in result.evidence}.values()
        )
        evidence_note_by_id: dict[str, str] = {}
        source_note_by_ref: dict[str, str] = {}
        evidence_paths: list[str] = []
        source_paths: list[str] = []

        for evidence in unique_evidence:
            if memory_id is None:
                evidence_note_by_id[evidence.evidence_id] = safe_note_id(
                    "Evidence",
                    evidence.evidence_id,
                )
            else:
                evidence_note_by_id[evidence.evidence_id] = managed_note_id(
                    "Evidence",
                    evidence.evidence_id,
                )
            source_note_by_ref.setdefault(evidence.source_ref, source_note_id(evidence))

        timestamp = self._timestamp() if memory_id is not None else None
        renderer = render_v2_report if report_body_markdown is not None else render_report
        renderer_kwargs = dict(
            report_note=report_note,
            evidence_notes=evidence_note_by_id,
            root_thread_id=identity.root_thread_id,
            memory_id=memory_id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        if report_body_markdown is not None:
            renderer_kwargs["architecture"] = report_architecture
        report_markdown = (
            renderer(brief, result, report_body_markdown, **renderer_kwargs)
            if report_body_markdown is not None
            else renderer(brief, result, **renderer_kwargs)
        )

        base_path = f"Memories/{memory_id}/" if memory_id is not None else ""

        with self._lock:
            for source_ref, source_note in source_note_by_ref.items():
                evidence = next(item for item in unique_evidence if item.source_ref == source_ref)
                relative_path = f"{base_path}sources/{source_note}.md"
                self._write_atomic(
                    relative_path,
                    render_source_note(
                        source_note,
                        evidence,
                        memory_id=memory_id,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                )
                source_paths.append(relative_path)

            for evidence in unique_evidence:
                evidence_note = evidence_note_by_id[evidence.evidence_id]
                source_note = source_note_by_ref[evidence.source_ref]
                relative_path = f"{base_path}evidence/{evidence_note}.md"
                self._write_atomic(
                    relative_path,
                    render_evidence_note(
                        evidence,
                        evidence_note=evidence_note,
                        source_note=source_note,
                        memory_id=memory_id,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                )
                evidence_paths.append(relative_path)

            report_path = f"{base_path}reports/{report_note}.md"
            self._write_atomic(report_path, report_markdown)

        manifest = MemoryManifest(
            report_path=report_path,
            evidence_paths=tuple(evidence_paths),
            source_paths=tuple(source_paths),
        )
        return report_markdown, manifest

    def read_text(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def replace_report(self, report_path: str, markdown: str) -> None:
        """Atomically replace one existing report without touching its bundle."""
        raw_path = str(report_path)
        if "\\" in raw_path:
            raise ValueError("report_path must use forward slashes")
        relative = PurePosixPath(raw_path)
        legacy_report = len(relative.parts) == 2 and relative.parts[0] == "reports"
        memory_report = (
            len(relative.parts) == 4
            and relative.parts[0] == "Memories"
            and relative.parts[2] == "reports"
        )
        valid_memory_id = False
        if memory_report:
            try:
                validate_memory_id(relative.parts[1])
                valid_memory_id = True
            except ValueError:
                pass
        if (
            relative.is_absolute()
            or not (legacy_report or (memory_report and valid_memory_id))
            or relative.suffix.lower() != ".md"
            or relative.name in {"", ".", ".."}
        ):
            raise ValueError(
                "report_path must match reports/*.md or Memories/M-id/reports/*.md"
            )
        if not markdown.strip():
            raise ValueError("replacement report cannot be empty")
        normalized = relative.as_posix()
        with self._lock:
            if not self._resolve(normalized).is_file():
                raise FileNotFoundError(f"report does not exist: {normalized}")
            self._write_atomic(normalized, markdown)


__all__ = [
    "MarkdownMemoryStore",
    "MemoryWriteConflictError",
    "update_memory_home_with_import",
    "update_memory_home_with_note",
]
