"""Filesystem-backed Markdown implementation of PaperPilot's one Memory Store."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path, PurePosixPath

from .models import ExecutionIdentity, MemoryManifest, ResearchBrief, ResearchResult
from .rendering import (
    render_evidence_note,
    render_report,
    render_source_note,
    report_note_id,
    safe_note_id,
    source_note_id,
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

    def persist_research(
        self,
        brief: ResearchBrief,
        result: ResearchResult,
        identity: ExecutionIdentity,
    ) -> tuple[str, MemoryManifest]:
        """Atomically replace stable note paths; repeated calls create no duplicates."""
        identity.validate()
        if identity.depth != 0:
            raise ValueError("only the root Research Agent can persist a final report")

        report_note = report_note_id(identity.root_thread_id)
        unique_evidence = list(
            {item.evidence_id: item for item in result.evidence}.values()
        )
        evidence_note_by_id: dict[str, str] = {}
        source_note_by_ref: dict[str, str] = {}
        evidence_paths: list[str] = []
        source_paths: list[str] = []

        for evidence in unique_evidence:
            evidence_note_by_id[evidence.evidence_id] = safe_note_id(
                "Evidence",
                evidence.evidence_id,
            )
            source_note_by_ref.setdefault(evidence.source_ref, source_note_id(evidence))

        report_markdown = render_report(
            brief,
            result,
            report_note=report_note,
            evidence_notes=evidence_note_by_id,
            root_thread_id=identity.root_thread_id,
        )

        with self._lock:
            for source_ref, source_note in source_note_by_ref.items():
                evidence = next(item for item in unique_evidence if item.source_ref == source_ref)
                relative_path = f"sources/{source_note}.md"
                self._write_atomic(
                    relative_path,
                    render_source_note(source_note, evidence),
                )
                source_paths.append(relative_path)

            for evidence in unique_evidence:
                evidence_note = evidence_note_by_id[evidence.evidence_id]
                source_note = source_note_by_ref[evidence.source_ref]
                relative_path = f"evidence/{evidence_note}.md"
                self._write_atomic(
                    relative_path,
                    render_evidence_note(
                        evidence,
                        evidence_note=evidence_note,
                        source_note=source_note,
                    ),
                )
                evidence_paths.append(relative_path)

            report_path = f"reports/{report_note}.md"
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
        relative = PurePosixPath(str(report_path))
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "reports"
            or relative.suffix.lower() != ".md"
            or relative.name in {"", ".", ".."}
        ):
            raise ValueError("report_path must match reports/*.md")
        if not markdown.strip():
            raise ValueError("replacement report cannot be empty")
        normalized = relative.as_posix()
        with self._lock:
            if not self._resolve(normalized).is_file():
                raise FileNotFoundError(f"report does not exist: {normalized}")
            self._write_atomic(normalized, markdown)
