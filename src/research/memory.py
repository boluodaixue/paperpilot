"""Filesystem-backed Markdown implementation of PaperPilot's one Memory Store."""
from __future__ import annotations

import os
import shutil
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
    memory_relative_path,
    validate_frontmatter,
    validate_memory_descriptor,
    validate_memory_id,
)


_MEMORY_DIRECTORIES = (
    "reports",
    "evidence",
    "sources",
    "notes",
    "imports",
    "attachments",
)


class MarkdownMemoryStore:
    """Persist reports, evidence, and sources as one idempotent Markdown bundle."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

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
        lines = markdown.splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("Memory Home.md must start with YAML frontmatter")
        try:
            closing = lines.index("---", 1)
        except ValueError as exc:
            raise ValueError("Memory Home.md frontmatter is not closed") from exc
        loaded = yaml.safe_load("\n".join(lines[1:closing]))
        if not isinstance(loaded, dict):
            raise ValueError("Memory Home.md frontmatter must be a mapping")
        return validate_frontmatter(loaded)

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
