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
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

from .models import (
    ExecutionIdentity,
    MemoryDescriptor,
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
    render_source_note,
    report_note_id,
    safe_note_id,
    source_note_id,
)
from .vault import (
    build_wikilink,
    memory_relative_path,
    resolve_vault_markdown_path,
    validate_frontmatter,
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
_HOME_SECTION_HEADING = re.compile(r"^##\s+")
_WIKILINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_HOME_RESTORE_EXCHANGES = 16
_VAULT_LOCKS: dict[str, threading.RLock] = {}
_VAULT_LOCKS_GUARD = threading.Lock()


class MemoryWriteConflictError(RuntimeError):
    """The Vault changed after a controlled Memory proposal was prepared."""


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
    def _write_commit_temp(parent: Path, name: str, content: str) -> Path:
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                dir=parent,
                prefix=f".{name}.",
                suffix=".tmp",
            ) as handle:
                handle.write(content.encode("utf-8"))
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
    ) -> tuple[str, MemoryManifest]:
        """Atomically replace stable note paths; repeated calls create no duplicates."""
        identity.validate()
        if identity.depth != 0:
            raise ValueError("only the root Research Agent can persist a final report")

        if memory_id is not None:
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
        report_markdown = render_report(
            brief,
            result,
            report_note=report_note,
            evidence_notes=evidence_note_by_id,
            root_thread_id=identity.root_thread_id,
            memory_id=memory_id,
            created_at=timestamp,
            updated_at=timestamp,
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
    "update_memory_home_with_note",
]
