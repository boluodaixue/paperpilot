"""Crash-consistent single-writer publication for the managed Markdown Vault.

The SQLite queue owns scheduling and fencing.  This module owns only canonical
command validation, same-volume preparation, filesystem publication, and
hash-driven recovery.  Markdown remains the durable knowledge source; command
payloads and private transaction files are transient recovery material.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .memory import _atomic_exchange, _legacy_snapshot, _windows_replace_file
from .vault import LEGACY_MEMORY_ID, scan_legacy_memory_markdown, validate_memory_id
from .vault_write_queue import (
    VAULT_WRITE_OPERATION_TYPES,
    VaultWriteJob,
    VaultWriteQueue,
    VaultWriterLease,
)

__all__ = [
    "VaultWriteCommandError",
    "VaultWriteConflict",
    "VaultWriter",
    "build_directory_create_command",
    "build_file_bundle_command",
    "canonical_command_hash",
]


_COMMAND_VERSION = 1
_MAX_RESTORE_EXCHANGES = 16
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_WRITER_DIRECTORY = ".paperpilot-writer"
_MARKER_NAMES = ("PREPARED", "TREE_READY", "LINEARIZED", "COMPLETED", "ARCHIVE_READY")
_EXPECTED_MODES = frozenset({"absent", "hash", "reuse"})
_MEMORY_DIRECTORIES = (
    "reports",
    "evidence",
    "sources",
    "notes",
    "imports",
    "attachments",
)


class VaultWriteCommandError(ValueError):
    """A queued writer command is malformed or violates the Vault contract."""


class VaultWriteConflict(RuntimeError):
    """Canonical Vault bytes no longer match the job's optimistic snapshot."""


@dataclass(frozen=True)
class _Target:
    path: str
    expected_mode: Literal["absent", "hash", "reuse"]
    expected_hash: str | None
    content: bytes
    content_hash: str


@dataclass(frozen=True)
class _Command:
    publish: Literal["file_bundle", "directory_create"]
    operation_type: str
    memory_id: str
    anchor: str
    directories: tuple[str, ...]
    targets: tuple[_Target, ...]
    input_hashes: dict[str, str]
    expected_home_hash: str | None
    result: dict[str, Any]


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VaultWriteCommandError("writer command must be canonical JSON data") from exc


def canonical_command_hash(command_blob: bytes) -> str:
    """Return the stable SHA-256 identity of one canonical command blob."""
    if not isinstance(command_blob, bytes):
        raise TypeError("command_blob must be bytes")
    decoded = _decode_command(command_blob)
    canonical = _encode_command(decoded)
    if canonical != command_blob:
        raise VaultWriteCommandError("command_blob is not canonical JSON")
    return _hash_bytes(command_blob)


def _relative_path(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise VaultWriteCommandError(f"{field_name} must be a non-empty POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value:
        raise VaultWriteCommandError(f"{field_name} must be a canonical relative path")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise VaultWriteCommandError(f"{field_name} contains an unsafe path segment")
    if pure.parts[:2] == ("Memories", _WRITER_DIRECTORY):
        raise VaultWriteCommandError(f"{field_name} targets private writer state")
    return value


def _json_result(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise VaultWriteCommandError("writer command result must be an object")
    normalized = json.loads(_canonical_json(dict(value)).decode("utf-8"))
    if not isinstance(normalized, dict):  # pragma: no cover - mapping invariant
        raise VaultWriteCommandError("writer command result must be an object")
    return normalized


def _input_hashes(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise VaultWriteCommandError("input_hashes must be an object")
    normalized: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip() or len(key) > 200:
            raise VaultWriteCommandError("input_hashes keys must be bounded strings")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise VaultWriteCommandError("input_hashes values must be SHA-256 digests")
        normalized[key] = digest
    return dict(sorted(normalized.items()))


def _operation_type(value: object, *, publish: str) -> str:
    if not isinstance(value, str) or value not in VAULT_WRITE_OPERATION_TYPES:
        raise VaultWriteCommandError("writer operation_type is unsupported")
    directory_operations = {"create_memory", "legacy_copy"}
    if (publish == "directory_create") != (value in directory_operations):
        raise VaultWriteCommandError("operation_type does not match publication kind")
    return value


def _validate_command_paths(
    *,
    operation_type: str,
    memory_id: str,
    publish: str,
    anchor: str,
    directories: Sequence[str],
    target_paths: Sequence[str],
) -> None:
    try:
        selected = validate_memory_id(memory_id)
    except ValueError as exc:
        raise VaultWriteCommandError("writer memory_id is invalid") from exc
    if selected == LEGACY_MEMORY_ID:
        raise VaultWriteCommandError("M-legacy is read-only")
    memory_root = PurePosixPath("Memories", selected)
    anchor_path = PurePosixPath(anchor)
    if publish == "directory_create":
        if anchor_path != memory_root:
            raise VaultWriteCommandError("directory anchor must equal the selected managed Memory root")
        expected_directories = {(memory_root / name).as_posix() for name in _MEMORY_DIRECTORIES}
        if set(directories) != expected_directories:
            raise VaultWriteCommandError("directory command must declare all six managed Memory directories")
    prefix = memory_root.parts
    for value in (*directories, *target_paths):
        if PurePosixPath(value).parts[: len(prefix)] != prefix:
            raise VaultWriteCommandError("writer command target crosses the selected Memory boundary")

    home = (memory_root / "Home.md").as_posix()
    if operation_type in {"memory_note", "memory_import"} and anchor != home:
        raise VaultWriteCommandError("note/import anchor must be selected Memory Home.md")
    if operation_type in {"research_bundle", "report_review"}:
        relative_anchor = anchor_path.relative_to(memory_root)
        if (
            len(relative_anchor.parts) != 2
            or relative_anchor.parts[0] != "reports"
            or relative_anchor.suffix.lower() != ".md"
        ):
            raise VaultWriteCommandError("research/report-review anchor must be a report")

    allowed_roots = {
        "research_bundle": {"reports", "evidence", "sources"},
        "report_review": {"reports"},
        "memory_note": {"notes"},
        "memory_import": {"attachments", "imports", "notes"},
        "create_memory": set(),
        "legacy_copy": {"reports", "evidence", "sources"},
    }[operation_type]
    non_home: list[PurePosixPath] = []
    for value in target_paths:
        if value == home:
            continue
        relative = PurePosixPath(value).relative_to(memory_root)
        if not relative.parts or relative.parts[0] not in allowed_roots:
            raise VaultWriteCommandError("writer target directory is invalid for its operation")
        non_home.append(relative)
    if operation_type == "create_memory" and list(target_paths) != [home]:
        raise VaultWriteCommandError("create_memory may contain only Home.md")
    if operation_type == "report_review" and list(target_paths) != [anchor]:
        raise VaultWriteCommandError("report_review may replace only its anchor report")
    if operation_type == "memory_note" and (len(non_home) != 1 or non_home[0].suffix.lower() != ".md"):
        raise VaultWriteCommandError("memory_note must create exactly one Markdown note")


def _target_payload(
    *,
    path: str,
    content: bytes,
    expected_mode: str,
    expected_hash: str | None,
) -> dict[str, object]:
    path = _relative_path(path, field_name="target path")
    if not isinstance(content, bytes):
        raise TypeError("target content must be bytes")
    if expected_mode not in _EXPECTED_MODES:
        raise VaultWriteCommandError("expected_mode must be absent, hash, or reuse")
    if expected_mode == "hash":
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise VaultWriteCommandError("hash targets require a lowercase SHA-256")
    elif expected_hash is not None:
        raise VaultWriteCommandError("only hash targets accept expected_hash")
    digest = _hash_bytes(content)
    return {
        "content_b64": base64.b64encode(content).decode("ascii"),
        "content_hash": digest,
        "expected_hash": expected_hash,
        "expected_mode": expected_mode,
        "path": path,
    }


def build_file_bundle_command(
    *,
    operation_type: str,
    memory_id: str,
    anchor_path: str,
    targets: Sequence[Mapping[str, object]],
    result: Mapping[str, object],
    input_hashes: Mapping[str, str] | None = None,
    expected_home_hash: str | None = None,
) -> bytes:
    """Build one canonical multi-file command whose anchor is published last.

    Each target mapping contains ``path``, byte ``content``, ``expected_mode``,
    and optionally ``expected_hash``.  ``reuse`` permits an absent target or an
    existing target whose bytes already equal the new content (attachments).
    """
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise VaultWriteCommandError("memory_id must be a non-empty string")
    anchor = _relative_path(anchor_path, field_name="anchor_path")
    encoded_targets: list[dict[str, object]] = []
    for raw in targets:
        if not isinstance(raw, Mapping) or set(raw) - {"path", "content", "expected_mode", "expected_hash"}:
            raise VaultWriteCommandError("file target fields do not match the contract")
        encoded_targets.append(
            _target_payload(
                path=raw.get("path"),  # type: ignore[arg-type]
                content=raw.get("content"),  # type: ignore[arg-type]
                expected_mode=str(raw.get("expected_mode") or ""),
                expected_hash=raw.get("expected_hash"),  # type: ignore[arg-type]
            )
        )
    paths = [str(item["path"]) for item in encoded_targets]
    if not paths or len(paths) != len(set(paths)):
        raise VaultWriteCommandError("file bundle paths must be non-empty and unique")
    if paths.count(anchor) != 1:
        raise VaultWriteCommandError("file bundle anchor must name exactly one target")
    operation_type = _operation_type(operation_type, publish="file_bundle")
    _validate_command_paths(
        operation_type=operation_type,
        memory_id=memory_id,
        publish="file_bundle",
        anchor=anchor,
        directories=(),
        target_paths=paths,
    )
    anchor_target = next(item for item in encoded_targets if item["path"] == anchor)
    if operation_type in {"memory_note", "memory_import"}:
        if PurePosixPath(anchor).name != "Home.md":
            raise VaultWriteCommandError("note/import bundles must use Home.md as anchor")
        if (
            not isinstance(expected_home_hash, str)
            or not _SHA256.fullmatch(expected_home_hash)
            or anchor_target["expected_mode"] != "hash"
            or anchor_target["expected_hash"] != expected_home_hash
        ):
            raise VaultWriteCommandError("note/import expected_home_hash must equal the anchor old hash")
    elif expected_home_hash is not None:
        raise VaultWriteCommandError("only note/import bundles accept expected_home_hash")
    blob = _canonical_json(
        {
            "anchor": anchor,
            "directories": [],
            "expected_home_hash": expected_home_hash,
            "input_hashes": _input_hashes(input_hashes),
            "memory_id": memory_id,
            "operation_type": operation_type,
            "publish": "file_bundle",
            "result": _json_result(result),
            "targets": sorted(encoded_targets, key=lambda item: str(item["path"])),
            "version": _COMMAND_VERSION,
        }
    )
    _decode_command(blob)
    return blob


def build_directory_create_command(
    *,
    operation_type: str,
    memory_id: str,
    anchor_directory: str,
    directories: Sequence[str],
    files: Sequence[Mapping[str, object]],
    result: Mapping[str, object],
    input_hashes: Mapping[str, str] | None = None,
) -> bytes:
    """Build a complete directory-create bundle published by one rename."""
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise VaultWriteCommandError("memory_id must be a non-empty string")
    anchor = _relative_path(anchor_directory, field_name="anchor_directory")
    if anchor.endswith("/"):
        raise VaultWriteCommandError("anchor_directory must not have a trailing slash")
    prefix = f"{anchor}/"
    normalized_directories = tuple(sorted(_relative_path(path, field_name="directory path") for path in directories))
    if len(normalized_directories) != len(set(normalized_directories)):
        raise VaultWriteCommandError("directory paths must be unique")
    if any(not path.startswith(prefix) for path in normalized_directories):
        raise VaultWriteCommandError("directory path must remain below its anchor")
    encoded_targets: list[dict[str, object]] = []
    for raw in files:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "content"}:
            raise VaultWriteCommandError("directory file fields do not match the contract")
        path = _relative_path(raw.get("path"), field_name="directory file path")
        if not path.startswith(prefix):
            raise VaultWriteCommandError("directory file must remain below its anchor")
        encoded_targets.append(
            _target_payload(
                path=path,
                content=raw.get("content"),  # type: ignore[arg-type]
                expected_mode="absent",
                expected_hash=None,
            )
        )
    paths = [str(item["path"]) for item in encoded_targets]
    if not paths or len(paths) != len(set(paths)):
        raise VaultWriteCommandError("directory files must be non-empty and unique")
    normalized_operation = _operation_type(operation_type, publish="directory_create")
    _validate_command_paths(
        operation_type=normalized_operation,
        memory_id=memory_id,
        publish="directory_create",
        anchor=anchor,
        directories=normalized_directories,
        target_paths=paths,
    )
    declared = set(normalized_directories)
    anchor_pure = PurePosixPath(anchor)
    for path in paths:
        parent = PurePosixPath(path).parent
        while parent != anchor_pure:
            if parent.as_posix() not in declared:
                raise VaultWriteCommandError("every directory file parent must be explicitly declared")
            parent = parent.parent
    blob = _canonical_json(
        {
            "anchor": anchor,
            "directories": list(normalized_directories),
            "expected_home_hash": None,
            "input_hashes": _input_hashes(input_hashes),
            "memory_id": memory_id,
            "operation_type": normalized_operation,
            "publish": "directory_create",
            "result": _json_result(result),
            "targets": sorted(encoded_targets, key=lambda item: str(item["path"])),
            "version": _COMMAND_VERSION,
        }
    )
    _decode_command(blob)
    return blob


def _decode_target(raw: object) -> _Target:
    if not isinstance(raw, Mapping) or set(raw) != {
        "content_b64",
        "content_hash",
        "expected_hash",
        "expected_mode",
        "path",
    }:
        raise VaultWriteCommandError("writer target fields do not match the contract")
    path = _relative_path(raw["path"], field_name="target path")
    mode = raw["expected_mode"]
    if mode not in _EXPECTED_MODES:
        raise VaultWriteCommandError("writer target expected_mode is invalid")
    expected_hash = raw["expected_hash"]
    if mode == "hash":
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise VaultWriteCommandError("writer target expected_hash is invalid")
    elif expected_hash is not None:
        raise VaultWriteCommandError("writer target has an unexpected expected_hash")
    encoded = raw["content_b64"]
    if not isinstance(encoded, str):
        raise VaultWriteCommandError("writer target content_b64 must be a string")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultWriteCommandError("writer target content_b64 is invalid") from exc
    content_hash = raw["content_hash"]
    if not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash) or _hash_bytes(content) != content_hash:
        raise VaultWriteCommandError("writer target content hash does not match bytes")
    return _Target(path, mode, expected_hash, content, content_hash)  # type: ignore[arg-type]


def _decode_command(blob: bytes) -> _Command:
    if not isinstance(blob, bytes):
        raise VaultWriteCommandError("writer command blob must be bytes")
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VaultWriteCommandError("writer command is not UTF-8 JSON") from exc
    if not isinstance(raw, Mapping) or set(raw) != {
        "anchor",
        "directories",
        "expected_home_hash",
        "input_hashes",
        "memory_id",
        "operation_type",
        "publish",
        "result",
        "targets",
        "version",
    }:
        raise VaultWriteCommandError("writer command fields do not match version 1")
    if raw["version"] != _COMMAND_VERSION:
        raise VaultWriteCommandError("writer command version is unsupported")
    publish = raw["publish"]
    if publish not in {"file_bundle", "directory_create"}:
        raise VaultWriteCommandError("writer command publication kind is unsupported")
    operation_type = _operation_type(raw["operation_type"], publish=str(publish))
    memory_id = raw["memory_id"]
    if not isinstance(memory_id, str) or not memory_id.strip():
        raise VaultWriteCommandError("writer command memory_id is invalid")
    anchor = _relative_path(raw["anchor"], field_name="command anchor")
    raw_directories = raw["directories"]
    if not isinstance(raw_directories, list) or any(not isinstance(path, str) for path in raw_directories):
        raise VaultWriteCommandError("writer command directories must be an array")
    directories = tuple(_relative_path(path, field_name="command directory") for path in raw_directories)
    if list(directories) != sorted(directories) or len(directories) != len(set(directories)):
        raise VaultWriteCommandError("writer command directories must be sorted and unique")
    if not isinstance(raw["targets"], list):
        raise VaultWriteCommandError("writer command targets must be an array")
    targets = tuple(_decode_target(value) for value in raw["targets"])
    paths = [target.path for target in targets]
    if not targets or paths != sorted(paths) or len(paths) != len(set(paths)):
        raise VaultWriteCommandError("writer command target paths must be sorted and unique")
    if publish == "file_bundle" and paths.count(anchor) != 1:
        raise VaultWriteCommandError("file bundle must contain its unique anchor")
    if publish == "file_bundle" and directories:
        raise VaultWriteCommandError("file bundle cannot declare directories")
    if publish == "directory_create":
        prefix = f"{anchor}/"
        if any(not target.path.startswith(prefix) for target in targets):
            raise VaultWriteCommandError("directory bundle target escapes its anchor")
        if any(target.expected_mode != "absent" for target in targets):
            raise VaultWriteCommandError("directory bundle targets must expect absence")
        if any(not path.startswith(prefix) for path in directories):
            raise VaultWriteCommandError("directory command directory escapes its anchor")
        declared = set(directories)
        anchor_pure = PurePosixPath(anchor)
        for target in targets:
            parent = PurePosixPath(target.path).parent
            while parent != anchor_pure:
                if parent.as_posix() not in declared:
                    raise VaultWriteCommandError("directory command omits a file parent directory")
                parent = parent.parent
    input_hashes = _input_hashes(raw["input_hashes"])
    expected_home_hash = raw["expected_home_hash"]
    if operation_type in {"memory_note", "memory_import"}:
        anchor_target = next(target for target in targets if target.path == anchor)
        if (
            PurePosixPath(anchor).name != "Home.md"
            or not isinstance(expected_home_hash, str)
            or not _SHA256.fullmatch(expected_home_hash)
            or anchor_target.expected_mode != "hash"
            or anchor_target.expected_hash != expected_home_hash
        ):
            raise VaultWriteCommandError("note/import Home hash contract is invalid")
    elif expected_home_hash is not None:
        raise VaultWriteCommandError("unexpected expected_home_hash")
    _validate_command_paths(
        operation_type=operation_type,
        memory_id=memory_id,
        publish=str(publish),
        anchor=anchor,
        directories=directories,
        target_paths=paths,
    )
    return _Command(
        publish=publish,  # type: ignore[arg-type]
        operation_type=operation_type,
        memory_id=memory_id,
        anchor=anchor,
        directories=directories,
        targets=targets,
        input_hashes=input_hashes,
        expected_home_hash=expected_home_hash,
        result=_json_result(raw["result"]),
    )


def _encode_command(command: _Command) -> bytes:
    return _canonical_json(
        {
            "anchor": command.anchor,
            "directories": list(command.directories),
            "expected_home_hash": command.expected_home_hash,
            "input_hashes": command.input_hashes,
            "memory_id": command.memory_id,
            "operation_type": command.operation_type,
            "publish": command.publish,
            "result": command.result,
            "targets": [
                {
                    "content_b64": base64.b64encode(target.content).decode("ascii"),
                    "content_hash": target.content_hash,
                    "expected_hash": target.expected_hash,
                    "expected_mode": target.expected_mode,
                    "path": target.path,
                }
                for target in command.targets
            ],
            "version": _COMMAND_VERSION,
        }
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Windows does not expose portable directory fsync. File handles are
        # still flushed before every replace and markers remain recoverable.
        pass
    finally:
        os.close(descriptor)


def _write_durable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_hash(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            return digest.hexdigest()
    except FileNotFoundError:
        return None


def _is_reparse(path: Path) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(stat, "st_file_attributes", 0) & 0x400)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _rename_no_replace(source: Path, target: Path) -> None:
    """Atomically move one filesystem entry only when target is absent."""
    if os.name == "nt":
        os.rename(source, target)
        return
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
            os.fsencode(source),
            -100,
            os.fsencode(target),
            1,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return
    raise OSError(errno.ENOTSUP, "atomic directory no-replace is unavailable")


class VaultWriter:
    """Execute and recover exactly one Vault scope under queue fencing."""

    def __init__(
        self,
        vault_root: str | os.PathLike[str],
        queue: VaultWriteQueue,
        *,
        failpoint: Callable[[str], None] | None = None,
        job_lease_seconds: float = 30.0,
        legacy_archive_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = Path(vault_root).resolve()
        self.queue = queue
        self.failpoint = failpoint
        self.job_lease_seconds = float(job_lease_seconds)
        self.legacy_archive_root = (
            None if legacy_archive_root is None else Path(legacy_archive_root).resolve(strict=False)
        )
        if self.job_lease_seconds <= 0:
            raise ValueError("job_lease_seconds must be positive")
        self.memories_root = self.root / "Memories"
        self.private_root = self.memories_root / _WRITER_DIRECTORY

    @staticmethod
    def _legacy_retirement(command: _Command) -> dict[str, Any] | None:
        raw = command.result.get("_legacy_retirement")
        if raw is None:
            return None
        if command.operation_type != "legacy_copy" or not isinstance(raw, Mapping):
            raise VaultWriteCommandError("legacy retirement is only valid for legacy_copy")
        required = {
            "archive_target",
            "archive_inventory",
            "dependency_hash",
            "migration_id",
            "path_mapping",
        }
        if set(raw) != required:
            raise VaultWriteCommandError("legacy retirement fields do not match the contract")
        archive_target = raw["archive_target"]
        migration_id = raw["migration_id"]
        dependency_hash = raw["dependency_hash"]
        mapping = raw["path_mapping"]
        inventory = raw["archive_inventory"]
        if not isinstance(archive_target, str) or not archive_target:
            raise VaultWriteCommandError("legacy archive target is invalid")
        if not isinstance(migration_id, str) or not re.fullmatch(r"LegacyMigration-[0-9a-f]{32}", migration_id):
            raise VaultWriteCommandError("legacy retirement migration_id is invalid")
        if not isinstance(dependency_hash, str) or not _SHA256.fullmatch(dependency_hash):
            raise VaultWriteCommandError("legacy dependency hash is invalid")
        if not isinstance(mapping, Mapping) or not mapping:
            raise VaultWriteCommandError("legacy retirement mapping is invalid")
        normalized_mapping: dict[str, str] = {}
        for source, target in mapping.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise VaultWriteCommandError("legacy retirement mapping paths are invalid")
            if PurePosixPath(source).parts[0] not in {"reports", "evidence", "sources"}:
                raise VaultWriteCommandError("legacy retirement source path is invalid")
            if PurePosixPath(target).parts[:2] != ("Memories", command.memory_id):
                raise VaultWriteCommandError("legacy retirement target path is invalid")
            normalized_mapping[source] = target
        if not isinstance(inventory, Mapping) or not inventory:
            raise VaultWriteCommandError("legacy archive inventory is invalid")
        normalized_inventory: dict[str, str | None] = {}
        for path, digest in inventory.items():
            if not isinstance(path, str) or not path:
                raise VaultWriteCommandError("legacy archive inventory path is invalid")
            pure = PurePosixPath(path.rstrip("/"))
            if pure.parts[0] not in {"reports", "evidence", "sources"} or ".." in pure.parts:
                raise VaultWriteCommandError("legacy archive inventory escapes its roots")
            if path.endswith("/"):
                if digest is not None:
                    raise VaultWriteCommandError("legacy archive directory cannot have a digest")
            elif not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise VaultWriteCommandError("legacy archive file digest is invalid")
            normalized_inventory[path] = digest
        return {
            "archive_target": archive_target,
            "archive_inventory": dict(sorted(normalized_inventory.items())),
            "dependency_hash": dependency_hash,
            "migration_id": migration_id,
            "path_mapping": dict(sorted(normalized_mapping.items())),
        }

    def _fail(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)

    def _fenced(
        self,
        lease: VaultWriterLease,
        job_id: str,
        action: Callable[[], Any],
    ) -> Any:
        if (
            self.queue.renew_job(
                job_id,
                lease,
                lease_seconds=self.job_lease_seconds,
            )
            is None
        ):
            raise RuntimeError("Vault Writer lease was lost")
        return self.queue.run_fenced(job_id, lease, action)

    def _job_root(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _SAFE_JOB_ID.fullmatch(job_id):
            raise VaultWriteCommandError("job_id is unsafe for private filesystem state")
        path = self.private_root / "jobs" / job_id
        self._assert_private_path(path)
        return path

    def _assert_private_path(self, path: Path) -> None:
        """Reject every linked/reparse component in Writer-owned storage."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise VaultWriteConflict("private Writer path escapes the Vault") from exc
        expected_prefix = Path("Memories") / _WRITER_DIRECTORY
        if relative != expected_prefix and not relative.is_relative_to(expected_prefix):
            raise VaultWriteConflict("private Writer path is outside its reserved root")
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                current.lstat()
            except FileNotFoundError:
                continue
            if current.is_symlink() or _is_reparse(current):
                raise VaultWriteConflict("private Writer path crosses a link or reparse point")
            try:
                resolved = current.resolve(strict=True)
            except OSError as exc:
                raise VaultWriteConflict("private Writer path cannot be resolved safely") from exc
            if not resolved.is_relative_to(self.root):
                raise VaultWriteConflict("private Writer path resolves outside the Vault")

    def _assert_private_tree(self, path: Path) -> None:
        self._assert_private_path(path)
        if not path.exists():
            return
        if not path.is_dir():
            raise VaultWriteConflict("private Writer tree is not a directory")
        for current_root, directories, files in os.walk(path, followlinks=False):
            current = Path(current_root)
            self._assert_private_path(current)
            for name in (*directories, *files):
                self._assert_private_path(current / name)

    def _remove_private_tree(self, path: Path) -> None:
        self._assert_private_tree(path)
        if path.exists():
            shutil.rmtree(path)
            _fsync_directory(path.parent)

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        current = path
        while not current.exists():
            if current == current.parent:
                raise VaultWriteConflict("path has no existing filesystem parent")
            current = current.parent
        return current

    def _assert_same_volume(self, private: Path, canonical: Path) -> None:
        private_parent = self._existing_parent(private)
        canonical_parent = self._existing_parent(canonical)
        if os.stat(private_parent).st_dev != os.stat(canonical_parent).st_dev:
            raise VaultWriteConflict("Writer staging and Vault target are on different volumes")
        if os.name == "nt":
            private_drive = os.path.splitdrive(str(private_parent.resolve()))[0].casefold()
            canonical_drive = os.path.splitdrive(str(canonical_parent.resolve()))[0].casefold()
            if private_drive != canonical_drive:
                raise VaultWriteConflict("Writer staging and Vault target are on different volumes")

    def _canonical(self, relative_path: str) -> Path:
        relative = _relative_path(relative_path, field_name="canonical target")
        current = self.root
        for part in PurePosixPath(relative).parts:
            candidate = current / part
            if _lexists(candidate) and (candidate.is_symlink() or _is_reparse(candidate)):
                raise VaultWriteConflict(f"Vault target crosses a linked path: {relative}")
            current = candidate
        resolved_parent = current.parent.resolve()
        if not resolved_parent.is_relative_to(self.root):
            raise VaultWriteCommandError("canonical target escapes the Vault root")
        return current

    def _ensure_private_root(self, lease: VaultWriterLease, job_id: str) -> None:
        def create() -> None:
            if self.memories_root.exists() and (self.memories_root.is_symlink() or _is_reparse(self.memories_root)):
                raise VaultWriteConflict("Memories root cannot be a linked path")
            self._assert_private_path(self.private_root)
            self._assert_private_path(self.private_root / "jobs")
            self.private_root.joinpath("jobs").mkdir(parents=True, exist_ok=True)
            self._assert_private_tree(self.private_root / "jobs")
            _fsync_directory(self.private_root.parent)

        self._fenced(lease, job_id, create)

    @staticmethod
    def _target_name(index: int) -> str:
        return f"{index:06d}"

    def _stage_path(self, job_root: Path, index: int) -> Path:
        path = job_root / "stage" / self._target_name(index)
        self._assert_private_path(path)
        return path

    def _backup_path(self, job_root: Path, index: int) -> Path:
        path = job_root / "backup" / self._target_name(index)
        self._assert_private_path(path)
        return path

    def _publish_path(self, job_root: Path, index: int) -> Path:
        path = job_root / "publish" / self._target_name(index)
        self._assert_private_path(path)
        return path

    def _marker(self, job_root: Path, name: str) -> Path:
        if name not in _MARKER_NAMES:
            raise ValueError("unknown writer marker")
        path = job_root / name
        self._assert_private_path(path)
        return path

    def _write_marker(
        self,
        lease: VaultWriterLease,
        job_id: str,
        job_root: Path,
        name: str,
        manifest_hash: str,
    ) -> None:
        marker = self._marker(job_root, name)
        if marker.exists():
            if not self._validate_marker(job_root, name, manifest_hash):
                raise VaultWriteConflict(f"writer {name} marker is invalid")
            return
        self._fenced(
            lease,
            job_id,
            lambda: _write_durable(marker, manifest_hash.encode("ascii")),
        )
        self._fail(f"after_{name.lower()}")

    def _manifest(self, job_root: Path) -> tuple[dict[str, Any], str]:
        path = job_root / "manifest.json"
        self._assert_private_path(path)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise VaultWriteConflict("prepared writer job has no manifest") from exc
        digest = _hash_bytes(content)
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise VaultWriteConflict("writer manifest is corrupt") from exc
        if not isinstance(manifest, dict) or _canonical_json(manifest) != content:
            raise VaultWriteConflict("writer manifest is not canonical")
        return manifest, digest

    def _validate_marker(self, job_root: Path, name: str, manifest_hash: str) -> bool:
        marker = self._marker(job_root, name)
        try:
            value = marker.read_text(encoding="ascii")
        except FileNotFoundError:
            return False
        if value != manifest_hash:
            raise VaultWriteConflict(f"writer {name} marker does not match manifest")
        return True

    def _validate_manifest(
        self,
        job: VaultWriteJob,
        command: _Command,
        manifest: Mapping[str, Any],
    ) -> None:
        if set(manifest) != {
            "anchor",
            "command_hash",
            "directories",
            "expected_home_hash",
            "input_hashes",
            "job_id",
            "memory_id",
            "operation_type",
            "publish",
            "targets",
            "version",
        }:
            raise VaultWriteConflict("writer manifest fields do not match the contract")
        expected_header = {
            "anchor": command.anchor,
            "command_hash": job.command_hash,
            "directories": list(command.directories),
            "expected_home_hash": command.expected_home_hash,
            "input_hashes": command.input_hashes,
            "job_id": job.job_id,
            "memory_id": command.memory_id,
            "operation_type": command.operation_type,
            "publish": command.publish,
            "version": _COMMAND_VERSION,
        }
        for key, value in expected_header.items():
            if manifest.get(key) != value:
                raise VaultWriteConflict(f"writer manifest {key} does not match command")
        raw_targets = manifest.get("targets")
        if not isinstance(raw_targets, list) or len(raw_targets) != len(command.targets):
            raise VaultWriteConflict("writer manifest target count does not match command")
        for index, (raw, command_target) in enumerate(zip(raw_targets, command.targets)):
            if not isinstance(raw, Mapping) or set(raw) != {
                "content_hash",
                "expected_hash",
                "expected_mode",
                "observed_hash",
                "path",
                "stage",
            }:
                raise VaultWriteConflict("writer manifest target fields are invalid")
            if (
                raw["path"] != command_target.path
                or raw["content_hash"] != command_target.content_hash
                or raw["expected_hash"] != command_target.expected_hash
                or raw["expected_mode"] != command_target.expected_mode
                or raw["stage"] != f"stage/{self._target_name(index)}"
            ):
                raise VaultWriteConflict("writer manifest target differs from command")
            observed = raw["observed_hash"]
            if observed is not None and (not isinstance(observed, str) or not _SHA256.fullmatch(observed)):
                raise VaultWriteConflict("writer manifest observed hash is invalid")
            if command_target.expected_mode == "absent" and observed is not None:
                raise VaultWriteConflict("absent target manifest has existing bytes")
            if command_target.expected_mode == "hash" and observed != command_target.expected_hash:
                raise VaultWriteConflict("hash target manifest has the wrong preimage")
            if command_target.expected_mode == "reuse" and observed not in {
                None,
                command_target.content_hash,
            }:
                raise VaultWriteConflict("reuse target manifest has foreign bytes")

    def _prepare(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        command: _Command,
    ) -> tuple[Path, dict[str, Any], str]:
        job_root = self._job_root(job.job_id)
        self._ensure_private_root(lease, job.job_id)
        if self._marker(job_root, "PREPARED").exists():
            manifest, digest = self._manifest(job_root)
            self._validate_marker(job_root, "PREPARED", digest)
            self._validate_manifest(job, command, manifest)
            return job_root, manifest, digest

        self._validate_external_inputs(command)

        if job_root.exists():
            self._fenced(
                lease,
                job.job_id,
                lambda: self._remove_private_tree(job_root),
            )
        self._fenced(
            lease,
            job.job_id,
            lambda: (job_root / "stage").mkdir(parents=True, exist_ok=False),
        )
        self._assert_private_tree(job_root)

        observed: list[dict[str, object]] = []
        for index, target in enumerate(command.targets):
            stage = self._stage_path(job_root, index)
            canonical = self._canonical(target.path)
            self._assert_same_volume(stage, canonical)
            self._fenced(
                lease,
                job.job_id,
                lambda stage=stage, content=target.content: _write_durable(stage, content),
            )
            self._fail(f"after_stage_file:{index}")
            current_hash = _read_hash(canonical)
            if target.expected_mode == "absent" and current_hash is not None:
                raise VaultWriteConflict(f"target was expected absent: {target.path}")
            if target.expected_mode == "hash" and current_hash != target.expected_hash:
                raise VaultWriteConflict(f"target changed before preparation: {target.path}")
            if target.expected_mode == "reuse" and current_hash not in {
                None,
                target.content_hash,
            }:
                raise VaultWriteConflict(f"reusable target contains different bytes: {target.path}")
            observed.append(
                {
                    "content_hash": target.content_hash,
                    "expected_hash": target.expected_hash,
                    "expected_mode": target.expected_mode,
                    "observed_hash": current_hash,
                    "path": target.path,
                    "stage": f"stage/{self._target_name(index)}",
                }
            )
            if current_hash is not None and current_hash != target.content_hash:
                backup = self._backup_path(job_root, index)
                content = canonical.read_bytes()
                if _hash_bytes(content) != current_hash:
                    raise VaultWriteConflict(f"target changed while backing up: {target.path}")
                self._fenced(
                    lease,
                    job.job_id,
                    lambda backup=backup, content=content: _write_durable(backup, content),
                )
                self._fail(f"after_backup:{index}")

        manifest = {
            "anchor": command.anchor,
            "command_hash": job.command_hash,
            "directories": list(command.directories),
            "expected_home_hash": command.expected_home_hash,
            "input_hashes": command.input_hashes,
            "job_id": job.job_id,
            "memory_id": command.memory_id,
            "operation_type": command.operation_type,
            "publish": command.publish,
            "targets": observed,
            "version": _COMMAND_VERSION,
        }
        manifest_content = _canonical_json(manifest)
        manifest_hash = _hash_bytes(manifest_content)
        self._fenced(
            lease,
            job.job_id,
            lambda: _write_durable(job_root / "manifest.json", manifest_content),
        )
        self._validate_manifest(job, command, manifest)
        self._write_marker(lease, job.job_id, job_root, "PREPARED", manifest_hash)
        return job_root, manifest, manifest_hash

    def _validate_external_inputs(self, command: _Command) -> None:
        """Recheck the only command input that remains externally mutable.

        Other input hashes identify immutable command values already embedded in
        the canonical blob. Legacy Markdown remains outside the command by W6's
        copy-only contract, so it must be rescanned before preparation and again
        immediately before the directory rename.
        """
        if command.operation_type != "legacy_copy":
            return
        expected = command.input_hashes.get("legacy_source")
        if expected is None:
            raise VaultWriteCommandError("legacy_copy requires legacy_source input hash")
        _, current = _legacy_snapshot(self.root)
        if current != expected:
            raise VaultWriteConflict("legacy source changed after preview")

    def _publish_addition(
        self,
        lease: VaultWriterLease,
        job_id: str,
        source: Path,
        target: Path,
        new_hash: str,
    ) -> None:
        def publish() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            current = _read_hash(target)
            if current == new_hash:
                return
            if current is not None:
                raise VaultWriteConflict("no-clobber target was concurrently created")
            try:
                os.link(source, target)
            except FileExistsError:
                if _read_hash(target) != new_hash:
                    raise VaultWriteConflict("no-clobber target was concurrently created")
            _fsync_directory(target.parent)

        self._fenced(lease, job_id, publish)

    def _replace_intent_path(self, job_root: Path, index: int) -> Path:
        path = job_root / "replace" / f"{self._target_name(index)}.json"
        self._assert_private_path(path)
        return path

    def _write_replace_intent(self, path: Path, intent: Mapping[str, Any]) -> None:
        self._assert_private_path(path)
        _write_durable(path, _canonical_json(intent))

    def _read_replace_intent(
        self,
        job_root: Path,
        path: Path,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_private_path(path)
        try:
            content = path.read_bytes()
            raw = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VaultWriteConflict("replace intent is unreadable") from exc
        fields = {
            "attempt",
            "desired",
            "desired_hash",
            "displaced",
            "expected_current_hash",
            "expected_hash",
            "index",
            "new_hash",
            "phase",
            "publish",
            "target",
            "version",
        }
        if not isinstance(raw, dict) or set(raw) != fields or _canonical_json(raw) != content:
            raise VaultWriteConflict("replace intent is not canonical")
        index = raw["index"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise VaultWriteConflict("replace intent index is invalid")
        targets = manifest.get("targets")
        if not isinstance(targets, list) or index >= len(targets):
            raise VaultWriteConflict("replace intent target is invalid")
        target_item = targets[index]
        if not isinstance(target_item, Mapping) or raw["target"] != target_item.get("path"):
            raise VaultWriteConflict("replace intent target differs from manifest")
        observed = target_item.get("observed_hash")
        content_hash = target_item.get("content_hash")
        if (raw["expected_hash"], raw["new_hash"]) not in {
            (observed, content_hash),
            (content_hash, observed),
        }:
            raise VaultWriteConflict("replace intent hashes differ from manifest")
        for key in ("expected_hash", "new_hash"):
            if not isinstance(raw[key], str) or not _SHA256.fullmatch(raw[key]):
                raise VaultWriteConflict("replace intent hash is invalid")
        expected_publish = f"publish/{self._target_name(index)}"
        expected_displaced = f"publish/{self._target_name(index)}.displaced" if os.name == "nt" else expected_publish
        if raw["publish"] != expected_publish or raw["displaced"] != expected_displaced:
            raise VaultWriteConflict("replace intent artifact path is invalid")
        if raw["phase"] not in {"exchange", "restore"}:
            raise VaultWriteConflict("replace intent phase is invalid")
        if (
            not isinstance(raw["attempt"], int)
            or isinstance(raw["attempt"], bool)
            or raw["attempt"] < 0
            or raw["attempt"] > _MAX_RESTORE_EXCHANGES
        ):
            raise VaultWriteConflict("replace intent attempt is invalid")
        for key in ("desired_hash", "expected_current_hash"):
            value = raw[key]
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                raise VaultWriteConflict("replace restore hash is invalid")
        desired = raw["desired"]
        if desired is not None:
            if not isinstance(desired, str) or not desired.startswith("publish/"):
                raise VaultWriteConflict("replace restore path is invalid")
            self._assert_private_path(job_root / desired)
        return raw

    def _remove_replace_artifact(self, path: Path) -> None:
        self._assert_private_path(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)

    def _restore_displaced_from_intent(
        self,
        job_root: Path,
        intent_path: Path,
        intent: dict[str, Any],
        target: Path,
    ) -> None:
        while int(intent["attempt"]) < _MAX_RESTORE_EXCHANGES:
            attempt = int(intent["attempt"])
            desired = job_root / str(intent["desired"])
            self._assert_private_path(desired)
            desired_hash = str(intent["desired_hash"])
            expected_current_hash = str(intent["expected_current_hash"])
            output = (
                self._publish_path(job_root, int(intent["index"])).with_name(
                    f"{self._target_name(int(intent['index']))}.restore-{attempt:02d}"
                )
                if os.name == "nt"
                else desired
            )
            self._assert_private_path(output)
            canonical_hash = _read_hash(target)
            desired_current_hash = _read_hash(desired)
            output_hash = _read_hash(output)
            exchanged = False
            if os.name == "nt":
                if desired_current_hash == desired_hash and output_hash is None:
                    _windows_replace_file(target, desired, output)
                    self._fail(f"after_replace_restore_exchange:{intent['index']}:{attempt}")
                    exchanged = True
                elif desired_current_hash is None and output_hash is not None and canonical_hash == desired_hash:
                    exchanged = True
            else:
                if canonical_hash == desired_hash and desired_current_hash is not None:
                    exchanged = True
                elif desired_current_hash == desired_hash:
                    _atomic_exchange(target, desired)
                    self._fail(f"after_replace_restore_exchange:{intent['index']}:{attempt}")
                    exchanged = True
                else:
                    raise VaultWriteConflict("replace restore artifact changed")
            if not exchanged:
                raise VaultWriteConflict("replace restore state is ambiguous")
            displaced_hash = _read_hash(output)
            if displaced_hash == expected_current_hash:
                self._remove_replace_artifact(output)
                self._remove_replace_artifact(intent_path)
                raise VaultWriteConflict("replace target changed at the linearization point")
            if displaced_hash is None:
                raise VaultWriteConflict("replace restore lost displaced content")
            intent = {
                **intent,
                "attempt": attempt + 1,
                "desired": output.relative_to(job_root).as_posix(),
                "desired_hash": displaced_hash,
                "expected_current_hash": desired_hash,
            }
            self._write_replace_intent(intent_path, intent)
        raise VaultWriteConflict(
            "replace target kept changing during rollback; latest content is "
            f"preserved under {job_root.relative_to(self.root).as_posix()}/publish"
        )

    def _settle_replace_intent(
        self,
        job_root: Path,
        intent_path: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        intent = self._read_replace_intent(job_root, intent_path, manifest)
        target = self._canonical(str(intent["target"]))
        if intent["phase"] == "restore":
            self._restore_displaced_from_intent(job_root, intent_path, intent, target)
            return
        publish = job_root / str(intent["publish"])
        displaced = job_root / str(intent["displaced"])
        self._assert_private_path(publish)
        self._assert_private_path(displaced)
        expected_hash = str(intent["expected_hash"])
        new_hash = str(intent["new_hash"])
        canonical_hash = _read_hash(target)
        publish_hash = _read_hash(publish)
        displaced_hash = _read_hash(displaced)
        if (
            canonical_hash == expected_hash
            and publish_hash == new_hash
            and (displaced == publish or displaced_hash is None)
        ):
            if os.name == "nt":
                _windows_replace_file(target, publish, displaced)
            else:
                _atomic_exchange(target, publish)
            self._fail(f"after_replace_exchange:{intent['index']}")
            canonical_hash = _read_hash(target)
            displaced_hash = _read_hash(displaced)
        elif canonical_hash not in {expected_hash, new_hash} and displaced_hash is None:
            self._remove_replace_artifact(publish)
            self._remove_replace_artifact(intent_path)
            raise VaultWriteConflict("replace target changed before atomic publication")
        if canonical_hash != new_hash or displaced_hash is None:
            raise VaultWriteConflict("replace exchange recovery state is ambiguous")
        if displaced_hash == expected_hash:
            self._remove_replace_artifact(displaced)
            self._remove_replace_artifact(intent_path)
            _fsync_directory(target.parent)
            return
        restoring = {
            **intent,
            "attempt": 0,
            "desired": displaced.relative_to(job_root).as_posix(),
            "desired_hash": displaced_hash,
            "expected_current_hash": new_hash,
            "phase": "restore",
        }
        self._write_replace_intent(intent_path, restoring)
        self._restore_displaced_from_intent(
            job_root,
            intent_path,
            restoring,
            target,
        )

    def _recover_replace_intents(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        intent_root = job_root / "replace"
        self._assert_private_path(intent_root)
        if not intent_root.is_dir():
            return
        self._assert_private_tree(intent_root)
        for intent_path in sorted(intent_root.glob("*.json"), key=lambda value: value.name):
            self._fenced(
                lease,
                job.job_id,
                lambda intent_path=intent_path: self._settle_replace_intent(
                    job_root,
                    intent_path,
                    manifest,
                ),
            )

    def _remove_intent_path(self, job_root: Path, index: int) -> Path:
        path = job_root / "remove" / f"{self._target_name(index)}.json"
        self._assert_private_path(path)
        return path

    def _write_remove_intent(self, path: Path, intent: Mapping[str, Any]) -> None:
        self._assert_private_path(path)
        _write_durable(path, _canonical_json(intent))

    def _read_remove_intent(
        self,
        job_root: Path,
        path: Path,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_private_path(path)
        try:
            content = path.read_bytes()
            raw = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VaultWriteConflict("remove intent is unreadable") from exc
        fields = {
            "artifact",
            "artifact_hash",
            "index",
            "new_hash",
            "phase",
            "target",
            "version",
        }
        if not isinstance(raw, dict) or set(raw) != fields or _canonical_json(raw) != content:
            raise VaultWriteConflict("remove intent is not canonical")
        index = raw["index"]
        targets = manifest.get("targets")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or not isinstance(targets, list)
            or index >= len(targets)
        ):
            raise VaultWriteConflict("remove intent index is invalid")
        target_item = targets[index]
        if (
            not isinstance(target_item, Mapping)
            or target_item.get("observed_hash") is not None
            or raw["target"] != target_item.get("path")
            or raw["new_hash"] != target_item.get("content_hash")
        ):
            raise VaultWriteConflict("remove intent differs from manifest")
        if not isinstance(raw["new_hash"], str) or not _SHA256.fullmatch(raw["new_hash"]):
            raise VaultWriteConflict("remove intent hash is invalid")
        expected_artifact = f"remove/{self._target_name(index)}.moved"
        if raw["artifact"] != expected_artifact:
            raise VaultWriteConflict("remove intent artifact path is invalid")
        if raw["phase"] not in {"move", "restore"}:
            raise VaultWriteConflict("remove intent phase is invalid")
        artifact_hash = raw["artifact_hash"]
        if artifact_hash is not None and (not isinstance(artifact_hash, str) or not _SHA256.fullmatch(artifact_hash)):
            raise VaultWriteConflict("remove intent artifact hash is invalid")
        if raw["phase"] == "move" and artifact_hash is not None:
            raise VaultWriteConflict("move intent cannot predeclare artifact bytes")
        if raw["phase"] == "restore" and artifact_hash is None:
            raise VaultWriteConflict("restore intent requires artifact bytes")
        self._assert_private_path(job_root / expected_artifact)
        return raw

    def _settle_remove_intent(
        self,
        job_root: Path,
        intent_path: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        intent = self._read_remove_intent(job_root, intent_path, manifest)
        target = self._canonical(str(intent["target"]))
        artifact = job_root / str(intent["artifact"])
        self._assert_private_path(artifact)
        new_hash = str(intent["new_hash"])

        if intent["phase"] == "move":
            artifact_hash = _read_hash(artifact)
            if artifact_hash is None:
                if _read_hash(target) is None:
                    self._remove_replace_artifact(intent_path)
                    raise VaultWriteConflict("rollback addition was externally deleted before atomic removal")
                artifact.parent.mkdir(parents=True, exist_ok=True)
                self._assert_private_tree(artifact.parent)
                try:
                    _rename_no_replace(target, artifact)
                except FileExistsError as exc:
                    raise VaultWriteConflict("remove quarantine artifact was concurrently created") from exc
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        raise VaultWriteConflict("remove quarantine artifact was concurrently created") from exc
                    raise
                self._fail(f"after_remove_rename:{intent['index']}")
                artifact_hash = _read_hash(artifact)
            if artifact_hash is None:
                raise VaultWriteConflict("atomic removal produced no quarantine artifact")
            self._fail(f"after_remove_check:{intent['index']}")
            canonical_after_move = _read_hash(target)
            if artifact_hash == new_hash:
                self._remove_replace_artifact(artifact)
                self._remove_replace_artifact(intent_path)
                if canonical_after_move is not None:
                    raise VaultWriteConflict("external content appeared after atomic rollback removal")
                return
            intent = {
                **intent,
                "artifact_hash": artifact_hash,
                "phase": "restore",
            }
            self._write_remove_intent(intent_path, intent)

        artifact_hash = str(intent["artifact_hash"])
        current_artifact_hash = _read_hash(artifact)
        canonical_hash = _read_hash(target)
        if current_artifact_hash == artifact_hash:
            if canonical_hash is not None:
                raise VaultWriteConflict("external canonical and quarantined rollback bytes were both preserved")
            try:
                _rename_no_replace(artifact, target)
            except FileExistsError as exc:
                raise VaultWriteConflict("external canonical appeared before no-clobber restore") from exc
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise VaultWriteConflict("external canonical appeared before no-clobber restore") from exc
                raise
            self._fail(f"after_remove_restore:{intent['index']}")
            current_artifact_hash = _read_hash(artifact)
            canonical_hash = _read_hash(target)
        if current_artifact_hash is None and canonical_hash == artifact_hash:
            self._remove_replace_artifact(intent_path)
            raise VaultWriteConflict("external content was restored after atomic rollback inspection")
        raise VaultWriteConflict("remove restore state is ambiguous; canonical and quarantine were preserved")

    def _recover_remove_intents(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        intent_root = job_root / "remove"
        self._assert_private_path(intent_root)
        if not intent_root.is_dir():
            return
        self._assert_private_tree(intent_root)
        for intent_path in sorted(intent_root.glob("*.json"), key=lambda value: value.name):
            self._fenced(
                lease,
                job.job_id,
                lambda intent_path=intent_path: self._settle_remove_intent(
                    job_root,
                    intent_path,
                    manifest,
                ),
            )

    def _remove_rollback_addition(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
        index: int,
    ) -> None:
        target_item = manifest["targets"][index]
        intent_path = self._remove_intent_path(job_root, index)
        intent = {
            "artifact": f"remove/{self._target_name(index)}.moved",
            "artifact_hash": None,
            "index": index,
            "new_hash": str(target_item["content_hash"]),
            "phase": "move",
            "target": str(target_item["path"]),
            "version": _COMMAND_VERSION,
        }

        def remove() -> None:
            self._write_remove_intent(intent_path, intent)
            self._fail(f"after_remove_intent:{index}")
            self._settle_remove_intent(job_root, intent_path, manifest)

        self._fenced(lease, job.job_id, remove)

    def _publish_replace(
        self,
        lease: VaultWriterLease,
        job_id: str,
        job_root: Path,
        index: int,
        source: Path,
        target: Path,
        expected_hash: str,
        new_hash: str,
    ) -> None:
        publish_copy = self._publish_path(job_root, index)
        intent_path = self._replace_intent_path(job_root, index)

        def publish() -> None:
            current = _read_hash(target)
            if current == new_hash:
                return
            if current != expected_hash:
                raise VaultWriteConflict("replace target changed before atomic publication")
            _write_durable(publish_copy, source.read_bytes())
            displaced = publish_copy.with_suffix(".displaced") if os.name == "nt" else publish_copy
            if displaced != publish_copy and _lexists(displaced):
                raise VaultWriteConflict("replace recovery path is already occupied")
            intent = {
                "attempt": 0,
                "desired": None,
                "desired_hash": None,
                "displaced": displaced.relative_to(job_root).as_posix(),
                "expected_current_hash": None,
                "expected_hash": expected_hash,
                "index": index,
                "new_hash": new_hash,
                "phase": "exchange",
                "publish": publish_copy.relative_to(job_root).as_posix(),
                "target": target.relative_to(self.root).as_posix(),
                "version": _COMMAND_VERSION,
            }
            self._write_replace_intent(intent_path, intent)
            manifest, _ = self._manifest(job_root)
            self._settle_replace_intent(job_root, intent_path, manifest)

        self._fenced(lease, job_id, publish)

    def _publish_file_target(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest_target: Mapping[str, object],
        index: int,
    ) -> None:
        target = self._canonical(str(manifest_target["path"]))
        stage = job_root / str(manifest_target["stage"])
        new_hash = str(manifest_target["content_hash"])
        if _read_hash(stage) != new_hash:
            raise VaultWriteConflict("writer staged target is missing or corrupt")
        observed = manifest_target.get("observed_hash")
        if observed is None or observed == new_hash:
            self._publish_addition(lease, job.job_id, stage, target, new_hash)
        else:
            self._publish_replace(
                lease,
                job.job_id,
                job_root,
                index,
                stage,
                target,
                str(observed),
                new_hash,
            )

    def _publish_file_bundle(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
        manifest_hash: str,
    ) -> None:
        targets = manifest["targets"]
        assert isinstance(targets, list)
        anchor = str(manifest["anchor"])
        ordered = [(index, target) for index, target in enumerate(targets) if target["path"] != anchor]
        anchor_item = next((index, target) for index, target in enumerate(targets) if target["path"] == anchor)
        for index, target in ordered:
            self._fail(f"before_publish_target:{index}")
            self._publish_file_target(job, lease, job_root, target, index)
            self._fail(f"after_publish_target:{index}")

        for target in targets:
            if target["path"] == anchor:
                continue
            if _read_hash(self._canonical(str(target["path"]))) != target["content_hash"]:
                raise VaultWriteConflict("non-anchor target changed before linearization")
        self._fail("before_anchor")
        self._publish_file_target(job, lease, job_root, anchor_item[1], anchor_item[0])
        self._fail("after_anchor")
        self._verify_file_bundle_new(manifest)
        self._write_marker(lease, job.job_id, job_root, "LINEARIZED", manifest_hash)
        self._verify_file_bundle_new(manifest)

    def _publish_directory(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
        manifest_hash: str,
    ) -> None:
        tree = job_root / "tree"
        self._assert_private_path(tree)
        tree_ready = self._validate_marker(job_root, "TREE_READY", manifest_hash)
        if tree_ready:
            self._assert_private_tree(tree)
            if not self._directory_matches(tree, manifest):
                raise VaultWriteConflict("TREE_READY private tree differs from manifest")
        else:
            if tree.exists():
                self._fenced(
                    lease,
                    job.job_id,
                    lambda: self._remove_private_tree(tree),
                )
            self._fenced(
                lease,
                job.job_id,
                lambda: tree.mkdir(parents=True, exist_ok=False),
            )
            self._fail("after_tree_mkdir")
            for directory in manifest["directories"]:
                relative = PurePosixPath(str(directory)).relative_to(PurePosixPath(str(manifest["anchor"])))
                destination = tree.joinpath(*relative.parts)
                self._assert_private_path(destination)
                self._fenced(
                    lease,
                    job.job_id,
                    lambda destination=destination: destination.mkdir(parents=True, exist_ok=True),
                )
            for index, target in enumerate(manifest["targets"]):
                relative = PurePosixPath(str(target["path"])).relative_to(PurePosixPath(str(manifest["anchor"])))
                destination = tree.joinpath(*relative.parts)
                self._assert_private_path(destination)
                stage = job_root / str(target["stage"])
                self._assert_private_path(stage)
                content = stage.read_bytes()
                if _hash_bytes(content) != target["content_hash"]:
                    raise VaultWriteConflict("directory stage differs from manifest")
                self._fenced(
                    lease,
                    job.job_id,
                    lambda destination=destination, content=content: _write_durable(destination, content),
                )
                self._fail(f"after_tree_file:{index}")
            self._assert_private_tree(tree)
            if not self._directory_matches(tree, manifest):
                raise VaultWriteConflict("private directory tree differs from manifest")
            self._write_marker(
                lease,
                job.job_id,
                job_root,
                "TREE_READY",
                manifest_hash,
            )
        anchor = self._canonical(str(manifest["anchor"]))
        self._assert_same_volume(tree, anchor)
        if anchor.exists():
            if self._directory_matches(anchor, manifest):
                self._write_marker(lease, job.job_id, job_root, "LINEARIZED", manifest_hash)
                return
            raise VaultWriteConflict("directory anchor already exists with different content")
        command = _Command(
            publish="directory_create",
            operation_type=str(manifest["operation_type"]),
            memory_id=str(manifest["memory_id"]),
            anchor=str(manifest["anchor"]),
            directories=tuple(str(path) for path in manifest["directories"]),
            targets=(),
            input_hashes={str(k): str(v) for k, v in manifest["input_hashes"].items()},
            expected_home_hash=manifest["expected_home_hash"],
            result={},
        )
        self._validate_external_inputs(command)
        self._assert_private_tree(tree)
        if not self._validate_marker(job_root, "TREE_READY", manifest_hash):
            raise VaultWriteConflict("directory tree is not ready for publication")
        if not self._directory_matches(tree, manifest):
            raise VaultWriteConflict("directory tree changed before publication")
        self._fail("before_anchor")

        def rename() -> None:
            anchor.parent.mkdir(parents=True, exist_ok=True)
            try:
                _rename_no_replace(tree, anchor)
            except FileExistsError as exc:
                raise VaultWriteConflict("directory anchor was concurrently created") from exc
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    raise VaultWriteConflict("directory anchor was concurrently created") from exc
                raise
            _fsync_directory(anchor.parent)

        self._fenced(lease, job.job_id, rename)
        self._fail("after_anchor")
        if not self._directory_matches(anchor, manifest):
            raise VaultWriteConflict("published directory does not match its manifest")
        self._write_marker(lease, job.job_id, job_root, "LINEARIZED", manifest_hash)

    def _directory_matches(self, anchor: Path, manifest: Mapping[str, Any]) -> bool:
        if not anchor.is_dir() or anchor.is_symlink() or _is_reparse(anchor):
            return False
        expected: dict[str, str] = {}
        anchor_pure = PurePosixPath(str(manifest["anchor"]))
        for target in manifest["targets"]:
            relative = PurePosixPath(str(target["path"])).relative_to(anchor_pure).as_posix()
            expected[relative] = str(target["content_hash"])
        actual: dict[str, str | None] = {}
        actual_directories: set[str] = set()
        try:
            for current_root, directories, files in os.walk(anchor, followlinks=False):
                current = Path(current_root)
                for name in directories:
                    path = current / name
                    if path.is_symlink() or _is_reparse(path):
                        return False
                    actual_directories.add(path.relative_to(anchor).as_posix())
                for name in files:
                    path = current / name
                    if path.is_symlink() or _is_reparse(path):
                        return False
                    actual[path.relative_to(anchor).as_posix()] = _read_hash(path)
        except OSError:
            return False
        expected_directories = {
            PurePosixPath(path).relative_to(anchor_pure).as_posix() for path in manifest["directories"]
        }
        return actual == expected and actual_directories == expected_directories

    def _verify_directory_new(self, manifest: Mapping[str, Any]) -> None:
        anchor = self._canonical(str(manifest["anchor"]))
        if not self._directory_matches(anchor, manifest):
            raise VaultWriteConflict("published directory was externally changed after linearization")

    def _anchor_state(self, manifest: Mapping[str, Any]) -> str:
        if manifest["publish"] == "directory_create":
            anchor = self._canonical(str(manifest["anchor"]))
            if not anchor.exists():
                return "old"
            return "new" if self._directory_matches(anchor, manifest) else "foreign"
        anchor_item = next(target for target in manifest["targets"] if target["path"] == manifest["anchor"])
        current = _read_hash(self._canonical(str(anchor_item["path"])))
        if current == anchor_item["content_hash"]:
            return "new"
        if current == anchor_item.get("observed_hash"):
            return "old"
        return "foreign"

    def _rollback_file_bundle(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
    ) -> None:
        foreign_paths: list[str] = []
        for index, target_item in reversed(list(enumerate(manifest["targets"]))):
            path = self._canonical(str(target_item["path"]))
            current = _read_hash(path)
            new_hash = str(target_item["content_hash"])
            observed = target_item.get("observed_hash")
            if current == observed:
                continue
            if current != new_hash:
                foreign_paths.append(str(target_item["path"]))
                continue
            if observed is None:
                try:
                    self._remove_rollback_addition(
                        job,
                        lease,
                        job_root,
                        manifest,
                        index,
                    )
                except VaultWriteConflict:
                    foreign_paths.append(str(target_item["path"]))
            else:
                backup = self._backup_path(job_root, index)
                if _read_hash(backup) != observed:
                    foreign_paths.append(str(target_item["path"]))
                    continue
                try:
                    self._publish_replace(
                        lease,
                        job.job_id,
                        job_root,
                        index,
                        backup,
                        path,
                        new_hash,
                        str(observed),
                    )
                except VaultWriteConflict:
                    foreign_paths.append(str(target_item["path"]))
        if foreign_paths:
            raise VaultWriteConflict("external content preserved during rollback: " + ", ".join(sorted(foreign_paths)))

    def _verify_file_bundle_new(self, manifest: Mapping[str, Any]) -> None:
        """Require the exact bundle and preserve every post-anchor deletion."""
        for target_item in manifest["targets"]:
            path = self._canonical(str(target_item["path"]))
            current = _read_hash(path)
            if current == target_item["content_hash"]:
                continue
            action = "deleted" if current is None else "changed"
            raise VaultWriteConflict(f"published Vault target was externally {action}: " f"{target_item['path']}")

    @staticmethod
    def _tree_inventory(base: Path) -> dict[str, str | None]:
        inventory: dict[str, str | None] = {}
        if not base.is_dir() or base.is_symlink() or _is_reparse(base):
            raise VaultWriteConflict("legacy archive tree is not a real directory")
        for current_root, directories, files in os.walk(base, followlinks=False):
            current = Path(current_root)
            for name in sorted(directories):
                path = current / name
                if path.is_symlink() or _is_reparse(path):
                    raise VaultWriteConflict("legacy archive contains a linked directory")
                inventory[f"{path.relative_to(base).as_posix()}/"] = None
            for name in sorted(files):
                if name == ".paperpilot-archive.json" and current == base:
                    continue
                path = current / name
                if path.is_symlink() or _is_reparse(path):
                    raise VaultWriteConflict("legacy archive contains a linked file")
                inventory[path.relative_to(base).as_posix()] = _hash_bytes(path.read_bytes())
        return dict(sorted(inventory.items()))

    def _archive_target(self, spec: Mapping[str, Any]) -> Path:
        if self.legacy_archive_root is None:
            raise VaultWriteConflict("legacy archive root is not configured")
        try:
            root = self.legacy_archive_root.resolve(strict=True)
        except OSError as exc:
            raise VaultWriteConflict("legacy archive root is unavailable") from exc
        if not root.is_dir() or root.is_symlink() or _is_reparse(root):
            raise VaultWriteConflict("legacy archive root is unsafe")
        if root == self.root or root.is_relative_to(self.root):
            raise VaultWriteConflict("legacy archive root must be outside the active Vault")
        raw = Path(str(spec["archive_target"]))
        target = raw.resolve(strict=False)
        scope = (root / self.queue.vault_scope).resolve(strict=False)
        if target.parent != scope or target.name != raw.name or not target.name:
            raise VaultWriteConflict("legacy archive target is outside its configured scope")
        return target

    def _archive_metadata(self, job: VaultWriteJob, spec: Mapping[str, Any]) -> bytes:
        return _canonical_json(
            {
                "archive_inventory": spec["archive_inventory"],
                "job_id": job.job_id,
                "memory_id": job.memory_id,
                "migration_id": spec["migration_id"],
                "vault_scope": self.queue.vault_scope,
            }
        )

    def _archive_is_owned(self, target: Path, job: VaultWriteJob, spec: Mapping[str, Any]) -> bool:
        try:
            metadata = (target / ".paperpilot-archive.json").read_bytes()
        except FileNotFoundError:
            return False
        return metadata == self._archive_metadata(job, spec) and self._tree_inventory(target) == spec["archive_inventory"]

    def _prepare_legacy_archive(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        spec: Mapping[str, Any],
        manifest_hash: str,
    ) -> Path:
        target = self._archive_target(spec)
        if target.exists():
            if not self._archive_is_owned(target, job, spec):
                raise VaultWriteConflict("legacy archive target already exists")
            self._write_marker(lease, job.job_id, job_root, "ARCHIVE_READY", manifest_hash)
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink() or _is_reparse(target.parent):
            raise VaultWriteConflict("legacy archive scope is unsafe")
        temporary = target.parent / f".{target.name}.{job.job_id}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        try:
            roots = {PurePosixPath(path.rstrip("/")).parts[0] for path in spec["archive_inventory"]}
            for name in sorted(roots):
                source = self.root / name
                if not source.is_dir() or source.is_symlink() or _is_reparse(source):
                    raise VaultWriteConflict(f"legacy archive source is unavailable: {name}")
                shutil.copytree(source, temporary / name, copy_function=shutil.copy2)
            if self._tree_inventory(temporary) != spec["archive_inventory"]:
                raise VaultWriteConflict("legacy source changed while creating its archive")
            _write_durable(temporary / ".paperpilot-archive.json", self._archive_metadata(job, spec))
            try:
                _rename_no_replace(temporary, target)
            except FileExistsError as exc:
                raise VaultWriteConflict("legacy archive target was concurrently created") from exc
            _fsync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        if not self._archive_is_owned(target, job, spec):
            raise VaultWriteConflict("legacy archive verification failed")
        self._write_marker(lease, job.job_id, job_root, "ARCHIVE_READY", manifest_hash)
        self._fail("after_archive_ready")
        return target

    def _hold_legacy_roots(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        spec: Mapping[str, Any],
    ) -> Path:
        hold = job_root / "legacy"
        self._assert_private_path(hold)
        self._fenced(lease, job.job_id, lambda: hold.mkdir(exist_ok=True))
        roots = {PurePosixPath(path.rstrip("/")).parts[0] for path in spec["archive_inventory"]}
        for name in sorted(roots):
            source = self.root / name
            preserved = hold / name
            self._assert_private_path(preserved)
            if source.exists() and preserved.exists():
                raise VaultWriteConflict("legacy root exists in active and held locations")
            if source.exists():
                self._assert_same_volume(preserved, source)
                self._fenced(
                    lease,
                    job.job_id,
                    lambda source=source, preserved=preserved: os.rename(source, preserved),
                )
                self._fail(f"after_legacy_hold:{name}")
            elif not preserved.exists():
                raise VaultWriteConflict(f"legacy root disappeared before retirement: {name}")
        if self._tree_inventory(hold) != spec["archive_inventory"]:
            raise VaultWriteConflict("held legacy roots differ from the confirmed inventory")
        return hold

    def _restore_held_legacy(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
    ) -> None:
        hold = job_root / "legacy"
        self._assert_private_path(hold)
        if not hold.is_dir():
            return
        for preserved in sorted(hold.iterdir(), key=lambda path: path.name, reverse=True):
            self._assert_private_path(preserved)
            target = self.root / preserved.name
            if target.exists():
                raise VaultWriteConflict("cannot restore legacy root over an existing path")
            self._fenced(
                lease,
                job.job_id,
                lambda preserved=preserved, target=target: os.rename(preserved, target),
            )
        try:
            hold.rmdir()
        except OSError:
            pass

    def _prepare_retirement_tree(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        job_root: Path,
        manifest: Mapping[str, Any],
        manifest_hash: str,
    ) -> Path:
        tree = job_root / "tree"
        self._assert_private_path(tree)
        if self._validate_marker(job_root, "TREE_READY", manifest_hash):
            if not self._directory_matches(tree, manifest):
                raise VaultWriteConflict("TREE_READY private tree differs from manifest")
            return tree
        if tree.exists():
            self._fenced(lease, job.job_id, lambda: self._remove_private_tree(tree))
        self._fenced(lease, job.job_id, lambda: tree.mkdir(parents=True, exist_ok=False))
        for directory in manifest["directories"]:
            relative = PurePosixPath(str(directory)).relative_to(PurePosixPath(str(manifest["anchor"])))
            destination = tree.joinpath(*relative.parts)
            self._fenced(
                lease,
                job.job_id,
                lambda destination=destination: destination.mkdir(parents=True, exist_ok=True),
            )
        for target in manifest["targets"]:
            relative = PurePosixPath(str(target["path"])).relative_to(PurePosixPath(str(manifest["anchor"])))
            destination = tree.joinpath(*relative.parts)
            stage = job_root / str(target["stage"])
            content = stage.read_bytes()
            if _hash_bytes(content) != target["content_hash"]:
                raise VaultWriteConflict("directory stage differs from manifest")
            self._fenced(
                lease,
                job.job_id,
                lambda destination=destination, content=content: _write_durable(destination, content),
            )
        if not self._directory_matches(tree, manifest):
            raise VaultWriteConflict("private retirement tree differs from manifest")
        self._write_marker(lease, job.job_id, job_root, "TREE_READY", manifest_hash)
        return tree

    def _publish_legacy_retirement(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        command: _Command,
        job_root: Path,
        manifest: Mapping[str, Any],
        manifest_hash: str,
        spec: Mapping[str, Any],
    ) -> None:
        anchor = self._canonical(str(manifest["anchor"]))
        tree = job_root / "tree"
        if not anchor.exists():
            tree = self._prepare_retirement_tree(
                job, lease, job_root, manifest, manifest_hash
            )
        archive = self._prepare_legacy_archive(job, lease, job_root, spec, manifest_hash)
        if anchor.exists():
            if not self._directory_matches(anchor, manifest):
                raise VaultWriteConflict("legacy migration target already differs")
        else:
            try:
                self._hold_legacy_roots(job, lease, job_root, spec)
                if scan_legacy_memory_markdown(self.root):
                    raise VaultWriteConflict("active Vault still contains legacy Markdown")
            except Exception:
                self._restore_held_legacy(job, lease, job_root)
                if self._archive_is_owned(archive, job, spec):
                    shutil.rmtree(archive)
                raise

        def publish() -> None:
            if anchor.exists():
                if not self._directory_matches(anchor, manifest):
                    raise VaultWriteConflict("legacy migration target already differs")
                return
            if not self._directory_matches(tree, manifest):
                raise VaultWriteConflict("private retirement tree changed before publication")
            anchor.parent.mkdir(parents=True, exist_ok=True)
            _rename_no_replace(tree, anchor)
            _fsync_directory(anchor.parent)
            if not self._directory_matches(anchor, manifest):
                raise VaultWriteConflict("published legacy migration tree differs")

        try:
            self.queue.commit_legacy_switch(
                job.job_id,
                lease,
                migration_id=str(spec["migration_id"]),
                memory_id=command.memory_id,
                archive_target=str(archive),
                path_mapping=spec["path_mapping"],
                expected_dependency_hash=str(spec["dependency_hash"]),
                publish=publish,
            )
        except Exception:
            if not anchor.exists():
                self._restore_held_legacy(job, lease, job_root)
                if self._archive_is_owned(archive, job, spec):
                    shutil.rmtree(archive)
            raise
        self._write_marker(lease, job.job_id, job_root, "LINEARIZED", manifest_hash)
        if scan_legacy_memory_markdown(self.root):
            raise VaultWriteConflict("legacy Markdown reappeared after retirement")
        if not self.queue.legacy_retirement_complete(
            str(spec["migration_id"]), command.memory_id, spec["path_mapping"]
        ):
            raise VaultWriteConflict("legacy retirement metadata is incomplete")

    def _complete(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
        command: _Command,
        job_root: Path,
        manifest_hash: str,
    ) -> VaultWriteJob:
        self._write_marker(lease, job.job_id, job_root, "COMPLETED", manifest_hash)
        public_result = {
            key: value
            for key, value in command.result.items()
            if key != "_legacy_retirement"
        }
        completed = self.queue.complete(job.job_id, lease, public_result)
        self._fail("after_db_success")
        self._cleanup_terminal(job_root, completed, lease)
        return completed

    def _cleanup_terminal(
        self,
        job_root: Path,
        job: VaultWriteJob,
        lease: VaultWriterLease,
    ) -> None:
        if job.status != "succeeded" or not job_root.is_dir():
            return
        manifest, digest = self._manifest(job_root)
        if manifest.get("job_id") != job.job_id:
            raise VaultWriteConflict("terminal journal belongs to another job")
        if not self._validate_marker(job_root, "COMPLETED", digest):
            return
        # Terminal jobs no longer have a job fence.  A writer-generation check
        # is sufficient because cleanup only removes this immutable private
        # journal; duplicate cleanup by a successor is harmless.
        self.queue.assert_fence(lease)
        self._remove_private_tree(job_root)

    def execute(self, job: VaultWriteJob, lease: VaultWriterLease) -> VaultWriteJob:
        """Execute or recover one claimed job and return its terminal queue row."""
        self.queue.assert_fence(lease, job_id=job.job_id)
        if not isinstance(job.command_blob, bytes):
            raise VaultWriteCommandError("nonterminal job has no command blob")
        command = _decode_command(job.command_blob)
        if _encode_command(command) != job.command_blob:
            raise VaultWriteCommandError("queued command is not canonical")
        if canonical_command_hash(job.command_blob) != job.command_hash:
            raise VaultWriteCommandError("queued command hash does not match command bytes")
        if command.operation_type != job.operation_type or command.memory_id != job.memory_id:
            raise VaultWriteCommandError("queued command identity does not match its job")
        job_root, manifest, manifest_hash = self._prepare(job, lease, command)
        self._recover_remove_intents(job, lease, job_root, manifest)
        self._recover_replace_intents(job, lease, job_root, manifest)
        retirement = self._legacy_retirement(command)
        if retirement is not None:
            state = self._anchor_state(manifest)
            if state == "foreign":
                raise VaultWriteConflict("legacy migration target contains third-party bytes")
            self._publish_legacy_retirement(
                job,
                lease,
                command,
                job_root,
                manifest,
                manifest_hash,
                retirement,
            )
            self._verify_directory_new(manifest)
            return self._complete(job, lease, command, job_root, manifest_hash)
        state = self._anchor_state(manifest)
        linearized = self._validate_marker(job_root, "LINEARIZED", manifest_hash)
        if state == "foreign":
            if command.publish == "file_bundle" and not linearized:
                self._rollback_file_bundle(job, lease, job_root, manifest)
            raise VaultWriteConflict("writer anchor contains third-party bytes")
        if state == "old" and linearized:
            raise VaultWriteConflict("linearized marker exists but anchor is old")
        if state == "old":
            if command.publish == "file_bundle":
                self._rollback_file_bundle(job, lease, job_root, manifest)
                self._publish_file_bundle(job, lease, job_root, manifest, manifest_hash)
            else:
                self._publish_directory(job, lease, job_root, manifest, manifest_hash)
        else:
            if command.publish == "file_bundle":
                self._verify_file_bundle_new(manifest)
            if not linearized:
                self._write_marker(lease, job.job_id, job_root, "LINEARIZED", manifest_hash)
            if command.publish == "file_bundle":
                self._verify_file_bundle_new(manifest)
        if command.publish == "file_bundle":
            self._verify_file_bundle_new(manifest)
        else:
            self._verify_directory_new(manifest)
        return self._complete(job, lease, command, job_root, manifest_hash)

    def _finish_conflict(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
    ) -> VaultWriteJob:
        discard_private = self._rollback_before_terminal_conflict(job, lease)
        terminal = self.queue.conflict(
            job.job_id,
            lease,
            error_code=("vault_conflict" if discard_private else "vault_conflict_quarantined"),
        )
        if discard_private:
            self._cleanup_terminal_private(self._job_root(job.job_id), lease)
        return terminal

    def _finish_invalid_command(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
    ) -> VaultWriteJob:
        try:
            job_root = self._job_root(job.job_id)
        except VaultWriteCommandError:
            return self.queue.fail(
                job.job_id,
                lease,
                error_code="invalid_command",
            )
        discard_private = not self._marker(job_root, "PREPARED").exists()
        terminal = self.queue.fail(
            job.job_id,
            lease,
            error_code=("invalid_command" if discard_private else "invalid_command_quarantined"),
        )
        if discard_private:
            self._cleanup_terminal_private(job_root, lease)
        return terminal

    def run_once(self, lease: VaultWriterLease) -> VaultWriteJob | None:
        """Claim and execute one queued/recoverable job under an active writer lease."""
        job = self.queue.claim_next(
            lease,
            lease_seconds=self.job_lease_seconds,
        )
        if job is None:
            return None
        try:
            return self.execute(job, lease)
        except VaultWriteConflict:
            return self._finish_conflict(job, lease)
        except VaultWriteCommandError:
            return self._finish_invalid_command(job, lease)

    def _rollback_before_terminal_conflict(
        self,
        job: VaultWriteJob,
        lease: VaultWriterLease,
    ) -> bool:
        """Converge every safely reversible pre-anchor change before terminal conflict."""
        if not isinstance(job.command_blob, bytes):
            return False
        try:
            command = _decode_command(job.command_blob)
            job_root = self._job_root(job.job_id)
            if not self._marker(job_root, "PREPARED").exists():
                return True
            manifest, digest = self._manifest(job_root)
            self._validate_marker(job_root, "PREPARED", digest)
            self._validate_manifest(job, command, manifest)
            remove_root = job_root / "remove"
            self._assert_private_path(remove_root)
            if remove_root.is_dir() and any(remove_root.iterdir()):
                return False
            intent_root = job_root / "replace"
            self._assert_private_path(intent_root)
            if intent_root.is_dir() and any(intent_root.glob("*.json")):
                return False
            state = self._anchor_state(manifest)
            if self._validate_marker(job_root, "LINEARIZED", digest):
                return False
            if command.publish == "file_bundle" and state != "new":
                try:
                    self._rollback_file_bundle(job, lease, job_root, manifest)
                except VaultWriteConflict:
                    pass
                for target_item in manifest["targets"]:
                    current = _read_hash(self._canonical(str(target_item["path"])))
                    if current == target_item["content_hash"] and current != target_item.get("observed_hash"):
                        return False
                if remove_root.is_dir() and any(remove_root.iterdir()):
                    return False
                return True
            return command.publish == "directory_create" and state == "old"
        except (OSError, RuntimeError, ValueError):
            # A third-party hash or damaged recovery artifact must be preserved.
            # The original conflict remains the public terminal classification.
            return False

    def _cleanup_terminal_private(
        self,
        job_root: Path,
        lease: VaultWriterLease,
    ) -> None:
        """Delete one proven-unneeded private journal after its terminal row."""
        if not job_root.is_dir():
            return
        self.queue.assert_fence(lease)
        self._remove_private_tree(job_root)

    def _orphan_has_published_anchor(self, job_root: Path) -> bool | None:
        """Return True/False for a trustworthy orphan, None for quarantine."""
        manifest_path = job_root / "manifest.json"
        prepared = job_root / "PREPARED"
        if not manifest_path.exists() and not prepared.exists():
            return False
        try:
            manifest, digest = self._manifest(job_root)
            if prepared.read_text(encoding="ascii") != digest:
                return None
            publish = manifest.get("publish")
            anchor = _relative_path(manifest.get("anchor"), field_name="orphan anchor")
            if publish == "directory_create":
                return self._canonical(anchor).exists()
            if publish != "file_bundle" or not isinstance(manifest.get("targets"), list):
                return None
            anchor_items = [
                target for target in manifest["targets"] if isinstance(target, Mapping) and target.get("path") == anchor
            ]
            if len(anchor_items) != 1:
                return None
            digest_value = anchor_items[0].get("content_hash")
            if not isinstance(digest_value, str) or not _SHA256.fullmatch(digest_value):
                return None
            return _read_hash(self._canonical(anchor)) == digest_value
        except (OSError, RuntimeError, ValueError):
            return None

    def recover(
        self,
        lease: VaultWriterLease,
        *,
        job_ids: Iterable[str] | None = None,
    ) -> tuple[VaultWriteJob, ...]:
        """Recover journals and optionally execute only a bounded job set.

        Orphan and terminal-journal housekeeping always scans the private Writer
        area.  Passing ``job_ids`` limits every nonterminal claim to that finite
        set; ``None`` retains the full-drain behavior used by explicit drains.
        """
        selected_order: tuple[str, ...] | None = None
        selected: set[str] | None = None
        if job_ids is not None:
            selected_order = tuple(dict.fromkeys(job_ids))
            if any(not isinstance(job_id, str) or not _SAFE_JOB_ID.fullmatch(job_id) for job_id in selected_order):
                raise ValueError("recovery job_id is unsafe")
            selected = set(selected_order)
        recovered: list[VaultWriteJob] = []
        seen: set[str] = set()
        jobs_root = self.private_root / "jobs"
        self._assert_private_path(jobs_root)
        if _lexists(jobs_root):
            self._assert_private_tree(jobs_root)
        job_roots = sorted(jobs_root.iterdir(), key=lambda path: path.name) if jobs_root.is_dir() else ()
        for job_root in job_roots:
            if not job_root.is_dir() or not _SAFE_JOB_ID.fullmatch(job_root.name):
                continue
            job = self.queue.get(job_root.name)
            if job is None:
                published = self._orphan_has_published_anchor(job_root)
                if published is False:
                    self.queue.assert_fence(lease)
                    self._remove_private_tree(job_root)
                continue
            if job.status == "succeeded":
                self._cleanup_terminal(job_root, job, lease)
                continue
            if job.status in {"conflict", "failed"}:
                continue
            if selected is not None and job.job_id not in selected:
                continue
            claimed = self.queue.claim_next(
                lease,
                lease_seconds=self.job_lease_seconds,
                job_id=job.job_id,
            )
            if claimed is None:
                continue
            seen.add(claimed.job_id)
            try:
                recovered.append(self.execute(claimed, lease))
            except VaultWriteConflict:
                recovered.append(self._finish_conflict(claimed, lease))
            except VaultWriteCommandError:
                recovered.append(self._finish_invalid_command(claimed, lease))
            # Unknown I/O/runtime errors intentionally leave the job running and
            # its command/journal intact for a later lease generation.
        pending_ids: Iterable[str | None]
        if selected_order is None:
            pending_ids = iter(lambda: None, object())
        else:
            pending_ids = selected_order
        for selected_id in pending_ids:
            if selected_id is not None and selected_id in seen:
                continue
            claimed = self.queue.claim_next(
                lease,
                lease_seconds=self.job_lease_seconds,
                job_id=selected_id,
            )
            if claimed is None:
                if selected_id is None:
                    break
                continue
            if claimed.job_id in seen:
                if selected_id is None:
                    break
                continue
            seen.add(claimed.job_id)
            try:
                recovered.append(self.execute(claimed, lease))
            except VaultWriteConflict:
                recovered.append(self._finish_conflict(claimed, lease))
            except VaultWriteCommandError:
                recovered.append(self._finish_invalid_command(claimed, lease))
        return tuple(recovered)
