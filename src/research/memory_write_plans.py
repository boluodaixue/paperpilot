"""Pure S2 plans for product-level managed Vault mutations and artifacts.

Planners may read canonical bytes to capture optimistic hashes. They never
write, rename, remove, or stage a Vault path. Command encoding has exactly one
owner: :mod:`src.research.vault_writer`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from .memory import MarkdownMemoryStore, update_memory_home_with_report
from .models import (
    ExecutionIdentity,
    MemoryDescriptor,
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
    source_note_id,
)
from .report_review import validate_revised_report
from .vault import (
    LEGACY_MEMORY_ID,
    build_attachment_wikilink,
    build_wikilink,
    memory_relative_path,
    validate_memory_descriptor,
    validate_memory_id,
)
from .vault_write_queue import VAULT_WRITE_OPERATION_TYPES
from .vault_writer import (
    build_directory_create_command,
    build_file_bundle_command,
    canonical_command_hash,
)

WriteOperation = Literal[
    "create_memory",
    "research_bundle",
    "report_review",
    "memory_note",
    "memory_import",
    "legacy_copy",
    "tool_artifact",
]
_MEMORY_DIRECTORIES = (
    "reports",
    "evidence",
    "sources",
    "notes",
    "imports",
    "attachments",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    return value


def _value_hash(value: Any) -> str:
    return _sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def create_memory_request_hash(*, memory_id: str, title: str) -> str:
    """Return the timestamp-independent identity of one create request."""
    return _value_hash(
        {
            "memory_id": _managed_memory(memory_id),
            "title": _required_text(title, field_name="title"),
        }
    )


def research_bundle_request_hash(
    brief: ResearchBrief,
    result: ResearchResult,
    identity: ExecutionIdentity,
    *,
    memory_id: str,
    report_body_markdown: str | None = None,
    report_architecture: str = "supervisor_v2",
) -> str:
    """Return the timestamp-independent identity of one research publication."""
    selected = _managed_memory(memory_id)
    identity.validate()
    if identity.depth != 0:
        raise ValueError("only the root Research Agent can persist a final report")
    if brief.memory_id not in {None, selected}:
        raise ValueError("research brief belongs to a different Memory")
    return _value_hash(
        {
            "brief": brief,
            "identity": identity,
            "memory_id": selected,
            "result": result,
            "report_body_markdown": report_body_markdown,
            "report_architecture": report_architecture,
        }
    )


def report_review_request_hash(
    *,
    memory_id: str,
    report_path: str,
    original_markdown: str,
    revised_markdown: str,
    manifest: MemoryManifest,
) -> str:
    """Return the stable identity of one deterministic report revision."""
    return _value_hash(
        {
            "manifest": manifest,
            "memory_id": _managed_memory(memory_id),
            "original_report": _sha256(original_markdown.encode("utf-8")),
            "report_path": report_path,
            "revised_report": _sha256(revised_markdown.encode("utf-8")),
        }
    )


def tool_artifact_content(
    artifact_id: str,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Any,
) -> bytes:
    """Encode the exact durable envelope used for one raw tool result."""
    artifact = _required_text(artifact_id, field_name="artifact_id")
    tool = _required_text(tool_name, field_name="tool_name")
    if not isinstance(arguments, Mapping):
        raise TypeError("tool artifact arguments must be a mapping")
    return json.dumps(
        {
            "artifact_id": artifact,
            "arguments": _jsonable(dict(arguments)),
            "result": _jsonable(result),
            "tool_name": tool,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_tool_artifact_plan(
    artifact_id: str,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Any,
    origin_thread_id: str,
    artifact_scope_id: str | None = None,
) -> MemoryWritePlan:
    """Build a content-addressed raw tool-result publication command."""
    thread = _required_text(origin_thread_id, field_name="origin_thread_id")
    scope = _required_text(artifact_scope_id or origin_thread_id, field_name="artifact_scope_id")
    content = tool_artifact_content(
        artifact_id,
        tool_name=tool_name,
        arguments=arguments,
        result=result,
    )
    thread_scope = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
    memory_id = f"M-artifacts-{thread_scope}"
    artifact_path = f"Artifacts/{thread_scope}/{artifact_id}.json"
    content_hash = _sha256(content)
    request_hash = _value_hash(
        {
            "artifact_id": artifact_id,
            "content_hash": content_hash,
            "origin_thread_id": thread,
            "artifact_scope_id": scope,
        }
    )
    command = build_file_bundle_command(
        operation_type="tool_artifact",
        memory_id=memory_id,
        anchor_path=artifact_path,
        targets=(
            _file(
                artifact_path,
                content,
                expected_mode="reuse",
            ),
        ),
        input_hashes={"request": request_hash},
        result={
            "artifact_id": artifact_id,
            "artifact_path": artifact_path,
            "content_hash": content_hash,
            "request_hash": request_hash,
            "size_bytes": len(content),
        },
    )
    return _plan(
        idempotency_key=f"tool-artifact:{thread_scope}:{artifact_id}",
        operation_type="tool_artifact",
        memory_id=memory_id,
        origin_thread_id=thread,
        command_blob=command,
    )


def _required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _managed_memory(memory_id: str) -> str:
    validate_memory_id(memory_id)
    if memory_id == LEGACY_MEMORY_ID:
        raise ValueError("M-legacy is read-only and cannot be a Writer target")
    return memory_id


def _job_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"VaultJob-{digest}"


@dataclass(frozen=True)
class MemoryWritePlan:
    """Queue metadata paired with the Writer-owned canonical command bytes."""

    job_id: str
    idempotency_key: str
    operation_type: WriteOperation
    memory_id: str
    origin_thread_id: str | None
    command_blob: bytes
    command_hash: str

    def __post_init__(self) -> None:
        _required_text(self.job_id, field_name="job_id")
        _required_text(self.idempotency_key, field_name="idempotency_key")
        if self.operation_type not in VAULT_WRITE_OPERATION_TYPES:
            raise ValueError("unsupported Vault write operation")
        _managed_memory(self.memory_id)
        if self.origin_thread_id is not None:
            _required_text(self.origin_thread_id, field_name="origin_thread_id")
        if not isinstance(self.command_blob, bytes) or not self.command_blob:
            raise ValueError("command_blob must be non-empty bytes")
        if canonical_command_hash(self.command_blob) != self.command_hash:
            raise ValueError("command_hash does not match canonical command_blob")

    def enqueue_kwargs(self) -> dict[str, object]:
        """Return exactly the opaque fields accepted by ``VaultWriteQueue``."""
        return {
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "operation_type": self.operation_type,
            "memory_id": self.memory_id,
            "origin_thread_id": self.origin_thread_id,
            "command_blob": self.command_blob,
            "command_hash": self.command_hash,
        }


def _plan(
    *,
    idempotency_key: str,
    operation_type: WriteOperation,
    memory_id: str,
    origin_thread_id: str | None,
    command_blob: bytes,
) -> MemoryWritePlan:
    return MemoryWritePlan(
        job_id=_job_id(idempotency_key),
        idempotency_key=idempotency_key,
        operation_type=operation_type,
        memory_id=memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command_blob,
        command_hash=canonical_command_hash(command_blob),
    )


def _file(
    path: str,
    content: str | bytes,
    *,
    expected_mode: Literal["absent", "hash", "reuse"],
    expected_hash: str | None = None,
) -> dict[str, object]:
    return {
        "path": path,
        "content": content.encode("utf-8") if isinstance(content, str) else content,
        "expected_mode": expected_mode,
        "expected_hash": expected_hash,
    }


def _snapshot_markdown(
    memory_store: MarkdownMemoryStore,
    path: str,
    desired: str,
) -> dict[str, object]:
    desired_bytes = desired.encode("utf-8")
    desired_hash = _sha256(desired_bytes)
    try:
        current = memory_store.read_text(path).encode("utf-8")
    except FileNotFoundError:
        return _file(path, desired_bytes, expected_mode="absent")
    current_hash = _sha256(current)
    if current_hash == desired_hash:
        return _file(path, desired_bytes, expected_mode="reuse")
    return _file(path, desired_bytes, expected_mode="hash", expected_hash=current_hash)


def _immutable_markdown(
    memory_store: MarkdownMemoryStore,
    path: str,
    desired: str,
) -> dict[str, object]:
    """Create generated knowledge once and preserve later Obsidian edits."""

    try:
        existing = memory_store.read_bytes(path)
    except FileNotFoundError:
        return _file(path, desired, expected_mode="absent")
    return _file(path, existing, expected_mode="reuse")


def build_create_memory_plan(
    *,
    memory_id: str,
    title: str,
    created_at: str,
    origin_thread_id: str | None = None,
) -> MemoryWritePlan:
    """Build a complete Memory directory command, including all six empty dirs."""
    memory_id = _managed_memory(memory_id)
    title = _required_text(title, field_name="title")
    created_at = _required_text(created_at, field_name="created_at")
    descriptor = validate_memory_descriptor(
        MemoryDescriptor(
            memory_id=memory_id,
            title=title,
            relative_path=memory_relative_path(memory_id),
            created_at=created_at,
            updated_at=created_at,
        )
    )
    memory_root = descriptor.relative_path.rstrip("/")
    home_path = f"{memory_root}/Home.md"
    idempotency_key = f"create-memory:{memory_id}"
    request_hash = create_memory_request_hash(memory_id=memory_id, title=title)
    command = build_directory_create_command(
        operation_type="create_memory",
        memory_id=memory_id,
        anchor_directory=memory_root,
        directories=[f"{memory_root}/{name}" for name in _MEMORY_DIRECTORIES],
        files=[
            {
                "path": home_path,
                "content": render_memory_home(
                    memory_id=memory_id,
                    title=title,
                    created_at=created_at,
                    updated_at=created_at,
                ).encode("utf-8"),
            }
        ],
        input_hashes={
            "descriptor": _value_hash(descriptor),
            "request": request_hash,
        },
        result={
            "memory_id": memory_id,
            "home_path": home_path,
            "request_hash": request_hash,
        },
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="create_memory",
        memory_id=memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command,
    )


def build_research_bundle_plan(
    memory_store: MarkdownMemoryStore,
    brief: ResearchBrief,
    result: ResearchResult,
    identity: ExecutionIdentity,
    *,
    memory_id: str,
    created_at: str,
    report_body_markdown: str | None = None,
    report_architecture: str = "supervisor_v2",
) -> MemoryWritePlan:
    """Build a managed research command with the report as its only anchor."""
    memory_id = _managed_memory(memory_id)
    memory_store.get_memory(memory_id)
    request_hash = research_bundle_request_hash(
        brief,
        result,
        identity,
        memory_id=memory_id,
        report_body_markdown=report_body_markdown,
        report_architecture=report_architecture,
    )
    created_at = _required_text(created_at, field_name="created_at")

    unique_evidence = list({item.evidence_id: item for item in result.evidence}.values())
    evidence_notes = {item.evidence_id: managed_note_id("Evidence", item.evidence_id) for item in unique_evidence}
    source_notes: dict[str, str] = {}
    for item in unique_evidence:
        source_notes.setdefault(item.source_ref, source_note_id(item))
    prefix = memory_relative_path(memory_id)
    targets: list[dict[str, object]] = []
    source_paths: list[str] = []
    for source_ref, source_note in source_notes.items():
        evidence = next(item for item in unique_evidence if item.source_ref == source_ref)
        path = f"{prefix}sources/{source_note}.md"
        source_paths.append(path)
        targets.append(
            _immutable_markdown(
                memory_store,
                path,
                render_source_note(
                    source_note,
                    evidence,
                    memory_id=memory_id,
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
        )
    evidence_paths: list[str] = []
    for evidence in unique_evidence:
        note_id = evidence_notes[evidence.evidence_id]
        path = f"{prefix}evidence/{note_id}.md"
        evidence_paths.append(path)
        targets.append(
            _immutable_markdown(
                memory_store,
                path,
                render_evidence_note(
                    evidence,
                    evidence_note=note_id,
                    source_note=source_notes[evidence.source_ref],
                    memory_id=memory_id,
                    created_at=created_at,
                    updated_at=created_at,
                ),
            )
        )
    report_id = report_note_id(identity.root_thread_id)
    report_path = f"{prefix}reports/{report_id}.md"
    renderer = render_v2_report if report_body_markdown is not None else render_report
    renderer_kwargs = dict(
        report_note=report_id,
        evidence_notes=evidence_notes,
        root_thread_id=identity.root_thread_id,
        memory_id=memory_id,
        created_at=created_at,
        updated_at=created_at,
    )
    if report_body_markdown is not None:
        renderer_kwargs["architecture"] = report_architecture
    report_markdown = (
        renderer(brief, result, report_body_markdown, **renderer_kwargs)
        if report_body_markdown is not None
        else renderer(brief, result, **renderer_kwargs)
    )
    targets.append(
        _snapshot_markdown(
            memory_store,
            report_path,
            report_markdown,
        )
    )
    home_path, home_markdown, home_hash = memory_store.memory_home_snapshot(memory_id)
    report_wikilink = build_wikilink(report_path)
    if report_wikilink in home_markdown:
        targets.append(_file(home_path, home_markdown, expected_mode="reuse"))
    else:
        updated_home = update_memory_home_with_report(
            home_markdown,
            report_wikilink,
            created_at,
        )
        targets.append(
            _file(
                home_path,
                updated_home,
                expected_mode="hash",
                expected_hash=home_hash,
            )
        )
    idempotency_key = f"research-bundle:{memory_id}:{identity.root_thread_id}"
    command = build_file_bundle_command(
        operation_type="research_bundle",
        memory_id=memory_id,
        anchor_path=report_path,
        targets=targets,
        input_hashes={
            "brief": _value_hash(brief),
            "identity": _value_hash(identity),
            "request": request_hash,
            "result": _value_hash(result),
            "report_body_markdown": _value_hash(report_body_markdown),
            "report_architecture": _value_hash(report_architecture),
        },
        result={
            "report_path": report_path,
            "evidence_paths": evidence_paths,
            "source_paths": source_paths,
            "request_hash": request_hash,
        },
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="research_bundle",
        memory_id=memory_id,
        origin_thread_id=identity.root_thread_id,
        command_blob=command,
    )


def build_report_review_plan(
    memory_store: MarkdownMemoryStore,
    *,
    memory_id: str,
    original_markdown: str,
    revised_markdown: str,
    manifest: MemoryManifest,
    origin_thread_id: str | None = None,
) -> MemoryWritePlan:
    """Build a report CAS whose expected old bytes are the review input."""
    memory_id = _managed_memory(memory_id)
    memory_store.get_memory(memory_id)
    report_path = PurePosixPath(manifest.report_path).as_posix()
    prefix = f"{memory_relative_path(memory_id)}reports/"
    if (
        report_path != manifest.report_path
        or not report_path.startswith(prefix)
        or PurePosixPath(report_path).suffix != ".md"
    ):
        raise ValueError("report review must target the selected managed Memory")
    validate_revised_report(original_markdown, revised_markdown, manifest)
    original_hash = _sha256(original_markdown.encode("utf-8"))
    revised_hash = _sha256(revised_markdown.encode("utf-8"))
    request_hash = report_review_request_hash(
        memory_id=memory_id,
        report_path=report_path,
        original_markdown=original_markdown,
        revised_markdown=revised_markdown,
        manifest=manifest,
    )
    idempotency_key = f"report-review:{memory_id}:{report_path}:{original_hash}"
    command = build_file_bundle_command(
        operation_type="report_review",
        memory_id=memory_id,
        anchor_path=report_path,
        targets=[
            _file(
                report_path,
                revised_markdown,
                expected_mode="hash",
                expected_hash=original_hash,
            )
        ],
        input_hashes={
            "original_report": original_hash,
            "request": request_hash,
            "revised_report": revised_hash,
        },
        result={
            "original_hash": original_hash,
            "report_path": report_path,
            "request_hash": request_hash,
            "revised_hash": revised_hash,
        },
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="report_review",
        memory_id=memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command,
    )


def build_memory_note_plan(
    memory_store: MarkdownMemoryStore,
    proposal: MemoryNoteProposal,
    *,
    origin_thread_id: str | None = None,
) -> MemoryWritePlan:
    """Build an absent note followed by the proposal-guarded Home anchor."""
    if not isinstance(proposal, MemoryNoteProposal):
        raise TypeError("proposal must be a MemoryNoteProposal")
    proposal = replace(proposal, source_paths=tuple(proposal.source_paths))
    memory_store.validate_memory_note_proposal(proposal)
    idempotency_key = f"memory-note:{proposal.memory_id}:{proposal.proposal_id}"
    command = build_file_bundle_command(
        operation_type="memory_note",
        memory_id=proposal.memory_id,
        anchor_path=proposal.home_path,
        targets=[
            _file(proposal.target_path, proposal.markdown, expected_mode="absent"),
            _file(
                proposal.home_path,
                proposal.home_markdown,
                expected_mode="hash",
                expected_hash=proposal.home_content_hash,
            ),
        ],
        input_hashes={"proposal": _value_hash(proposal)},
        expected_home_hash=proposal.home_content_hash,
        result={
            "memory_id": proposal.memory_id,
            "target_path": proposal.target_path,
            "home_path": proposal.home_path,
            "wikilink": proposal.wikilink,
        },
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="memory_note",
        memory_id=proposal.memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command,
    )


def build_memory_import_plan(
    memory_store: MarkdownMemoryStore,
    proposal: MemoryImportProposal,
    *,
    origin_thread_id: str | None = None,
) -> MemoryWritePlan:
    """Build attachment/import/note files followed by the guarded Home anchor."""
    if not isinstance(proposal, MemoryImportProposal):
        raise TypeError("proposal must be a MemoryImportProposal")
    proposal = replace(proposal, note_source_paths=tuple(proposal.note_source_paths))
    memory_store.validate_memory_import_proposal(proposal)
    idempotency_key = f"memory-import:{proposal.memory_id}:{proposal.import_id}"
    command = build_file_bundle_command(
        operation_type="memory_import",
        memory_id=proposal.memory_id,
        anchor_path=proposal.home_path,
        targets=[
            _file(
                proposal.attachment_path,
                proposal.attachment_bytes,
                expected_mode="reuse",
            ),
            _file(proposal.import_path, proposal.import_markdown, expected_mode="absent"),
            _file(proposal.note_path, proposal.note_markdown, expected_mode="absent"),
            _file(
                proposal.home_path,
                proposal.home_markdown,
                expected_mode="hash",
                expected_hash=proposal.home_content_hash,
            ),
        ],
        input_hashes={
            "attachment": proposal.content_hash,
            "proposal": _value_hash(proposal),
        },
        expected_home_hash=proposal.home_content_hash,
        result={
            "status": "committed",
            "memory_id": proposal.memory_id,
            "attachment_path": proposal.attachment_path,
            "import_path": proposal.import_path,
            "note_path": proposal.note_path,
            "home_path": proposal.home_path,
            "wikilinks": [
                proposal.import_wikilink,
                build_attachment_wikilink(proposal.attachment_path),
                proposal.note_wikilink,
            ],
        },
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="memory_import",
        memory_id=proposal.memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command,
    )


def build_legacy_copy_plan(
    memory_store: MarkdownMemoryStore,
    proposal: Mapping[str, object],
    *,
    origin_thread_id: str | None = None,
) -> MemoryWritePlan:
    """Build W6 copy publication, optionally followed by the S3 retirement switch."""
    normalized = dict(proposal)
    retirement = normalized.pop("retirement", None)
    files = normalized.get("files")
    if isinstance(files, list):
        normalized["files"] = tuple(dict(item) for item in files)
    descriptor = memory_store._validate_legacy_migration_documents(normalized)
    memory_root = descriptor.relative_path.rstrip("/")
    directory_files: list[dict[str, object]] = [
        {
            "path": str(normalized["home_path"]),
            "content": str(normalized["home_markdown"]).encode("utf-8"),
        }
    ]
    normalized_files = normalized["files"]
    if not isinstance(normalized_files, tuple):
        raise ValueError("legacy migration proposal files must be a tuple")
    for item in normalized_files:
        if not isinstance(item, Mapping):
            raise ValueError("legacy migration file entry is invalid")
        directory_files.append(
            {
                "path": str(item["target_path"]),
                "content": str(item["markdown"]).encode("utf-8"),
            }
        )
    retirement_payload: dict[str, object] | None = None
    if retirement is not None:
        if not isinstance(retirement, Mapping):
            raise ValueError("legacy retirement preview is invalid")
        if retirement.get("blocked_reason") is not None:
            raise ValueError(str(retirement["blocked_reason"]))
        required = {
            "archive_target",
            "archive_inventory",
            "affected_sessions",
            "affected_manifests",
            "dependency_hash",
            "path_mapping",
        }
        if set(retirement) != required:
            raise ValueError("legacy retirement preview fields do not match the contract")
        path_mapping = retirement.get("path_mapping")
        expected_mapping = {
            str(item["source_path"]): str(item["target_path"]) for item in normalized_files if isinstance(item, Mapping)
        }
        if not isinstance(path_mapping, Mapping) or dict(path_mapping) != expected_mapping:
            raise ValueError("legacy retirement path mapping differs from migration files")
        retirement_payload = {
            "archive_target": retirement["archive_target"],
            "archive_inventory": retirement["archive_inventory"],
            "dependency_hash": retirement["dependency_hash"],
            "migration_id": normalized["proposal_id"],
            "path_mapping": dict(path_mapping),
        }
    idempotency_key = f"legacy-copy:{descriptor.memory_id}:{normalized['proposal_id']}"
    result: dict[str, object] = {
        "status": "committed",
        "source_memory_id": LEGACY_MEMORY_ID,
        "memory_id": descriptor.memory_id,
        "home_path": str(normalized["home_path"]),
    }
    if retirement_payload is not None:
        result["_legacy_retirement"] = retirement_payload
    command = build_directory_create_command(
        operation_type="legacy_copy",
        memory_id=descriptor.memory_id,
        anchor_directory=memory_root,
        directories=[f"{memory_root}/{name}" for name in _MEMORY_DIRECTORIES],
        files=directory_files,
        input_hashes={
            "legacy_source": str(normalized["source_content_hash"]),
            "proposal": _value_hash(dict(proposal)),
        },
        result=result,
    )
    return _plan(
        idempotency_key=idempotency_key,
        operation_type="legacy_copy",
        memory_id=descriptor.memory_id,
        origin_thread_id=origin_thread_id,
        command_blob=command,
    )


plan_create_memory = build_create_memory_plan
plan_research_bundle = build_research_bundle_plan
plan_report_review = build_report_review_plan
plan_memory_note = build_memory_note_plan
plan_memory_import = build_memory_import_plan
plan_legacy_copy = build_legacy_copy_plan


__all__ = [
    "MemoryWritePlan",
    "build_create_memory_plan",
    "build_legacy_copy_plan",
    "build_memory_import_plan",
    "build_memory_note_plan",
    "build_report_review_plan",
    "build_research_bundle_plan",
    "create_memory_request_hash",
    "plan_create_memory",
    "plan_legacy_copy",
    "plan_memory_import",
    "plan_memory_note",
    "plan_report_review",
    "plan_research_bundle",
    "report_review_request_hash",
    "research_bundle_request_hash",
]
