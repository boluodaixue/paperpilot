"""Pure Memory/Vault contract validation and read-only legacy recognition."""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from .models import MemoryDescriptor


LEGACY_MEMORY_ID = "M-legacy"
LEGACY_ROOT_DIRECTORIES = ("reports", "evidence", "sources")

_MEMORY_ID = re.compile(r"^M-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_NOTE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_ATTACHMENT_NAME = re.compile(r"^Asset-[0-9a-f]{64}\.(?:pdf|txt|html)$")
_FRONTMATTER_REQUIRED = frozenset(
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
_PROPERTY_VALUE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ORIGINS = frozenset({"user", "research", "import", "conversation"})
_STATUSES = frozenset({"draft", "confirmed"})
_INVALID_WIKILINK_CHARACTERS = frozenset("[]|#^\r\n")
_INVALID_WINDOWS_PATH_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def validate_memory_id(memory_id: str) -> str:
    """Return a valid stable Memory ID, or raise ``ValueError``."""
    if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
        raise ValueError(
            "memory_id must start with 'M-' and contain only ASCII letters, "
            "digits, and single hyphen separators"
        )
    return memory_id


def memory_relative_path(memory_id: str) -> str:
    """Map a stable Memory ID to its canonical Vault-relative directory."""
    return f"Memories/{validate_memory_id(memory_id)}/"


def _parse_aware_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a timezone-aware ISO-8601 string")
    parse_value = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a timezone-aware ISO-8601 string"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an ISO-8601 timezone offset")
    return parsed


def validate_memory_descriptor(descriptor: MemoryDescriptor) -> MemoryDescriptor:
    """Validate the minimal descriptor without deriving identity from its title."""
    if not isinstance(descriptor, MemoryDescriptor):
        raise TypeError("descriptor must be a MemoryDescriptor")
    validate_memory_id(descriptor.memory_id)
    if not isinstance(descriptor.title, str) or not descriptor.title.strip():
        raise ValueError("Memory title must be a non-empty string")
    expected_path = memory_relative_path(descriptor.memory_id)
    if descriptor.relative_path != expected_path:
        raise ValueError(f"relative_path must be the canonical path {expected_path!r}")
    created_at = _parse_aware_timestamp(descriptor.created_at, field_name="created_at")
    updated_at = _parse_aware_timestamp(descriptor.updated_at, field_name="updated_at")
    if updated_at < created_at:
        raise ValueError("updated_at cannot be earlier than created_at")
    return descriptor


def ensure_unique_memory_ids(
    memories: Iterable[MemoryDescriptor | str],
) -> tuple[str, ...]:
    """Validate an iterable and reject duplicate stable Memory IDs."""
    memory_ids: list[str] = []
    seen: set[str] = set()
    for memory in memories:
        if isinstance(memory, MemoryDescriptor):
            memory_id = validate_memory_descriptor(memory).memory_id
        else:
            memory_id = validate_memory_id(memory)
        if memory_id in seen:
            raise ValueError(f"duplicate memory_id: {memory_id}")
        seen.add(memory_id)
        memory_ids.append(memory_id)
    return tuple(memory_ids)


def _validate_flat_frontmatter_value(key: str, value: object) -> None:
    if isinstance(value, Mapping):
        raise ValueError(f"frontmatter field {key!r} cannot contain a nested mapping")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"frontmatter field {key!r} cannot contain a sequence")
    if not isinstance(value, (str, int, float, bool)) and value is not None:
        raise ValueError(f"frontmatter field {key!r} must be a YAML scalar")


def validate_frontmatter(frontmatter: Mapping[str, object]) -> dict[str, object]:
    """Validate flat YAML properties for a PaperPilot-managed Markdown note."""
    if not isinstance(frontmatter, Mapping):
        raise TypeError("frontmatter must be a mapping")
    if any(not isinstance(key, str) or not key for key in frontmatter):
        raise ValueError("frontmatter keys must be non-empty strings")
    missing = sorted(_FRONTMATTER_REQUIRED.difference(frontmatter))
    if missing:
        raise ValueError(f"frontmatter is missing required fields: {', '.join(missing)}")

    note_id = frontmatter["id"]
    if not isinstance(note_id, str) or not _NOTE_ID.fullmatch(note_id):
        raise ValueError(
            "frontmatter id must be a stable ASCII ID with a prefix and hyphen"
        )
    validate_memory_id(frontmatter["memory_id"])  # type: ignore[arg-type]

    note_type = frontmatter["type"]
    if not isinstance(note_type, str) or not _PROPERTY_VALUE.fullmatch(note_type):
        raise ValueError("frontmatter type must be a stable lowercase identifier")
    title = frontmatter["title"]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("frontmatter title must be a non-empty string")
    origin = frontmatter["origin"]
    if not isinstance(origin, str) or origin not in _ORIGINS:
        raise ValueError(f"frontmatter origin must be one of {sorted(_ORIGINS)}")
    status = frontmatter["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError(f"frontmatter status must be one of {sorted(_STATUSES)}")

    created_at = _parse_aware_timestamp(frontmatter["created_at"], field_name="created_at")
    updated_at = _parse_aware_timestamp(frontmatter["updated_at"], field_name="updated_at")
    if updated_at < created_at:
        raise ValueError("updated_at cannot be earlier than created_at")

    tags = frontmatter["tags"]
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("frontmatter tags must be a list of strings")

    for key, value in frontmatter.items():
        if key == "tags":
            continue
        _validate_flat_frontmatter_value(key, value)
    return dict(frontmatter)


def _coerce_relative_path(relative_path: str | os.PathLike[str]) -> str:
    raw = os.fspath(relative_path)
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise ValueError("Vault path must be a non-empty relative string")
    if "\\" in raw:
        raise ValueError("Vault-relative paths must use forward slashes")
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError("Vault path must be relative")
    if PureWindowsPath(raw).drive:
        raise ValueError("Vault path cannot contain a Windows drive or UNC root")
    return raw


def _validate_path_components(components: Sequence[str], *, field_name: str) -> None:
    for component in components:
        if component in {"", ".", ".."}:
            raise ValueError(f"{field_name} cannot contain empty, '.' or '..' components")
        if component.endswith((" ", ".")):
            raise ValueError(f"{field_name} components cannot end with a space or dot")
        if any(
            character in _INVALID_WINDOWS_PATH_CHARACTERS
            or ord(character) < 32
            or ord(character) == 127
            for character in component
        ):
            raise ValueError(f"{field_name} contains an unsafe Windows path character")
        device_stem = component.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"{field_name} contains a reserved Windows device name")


def resolve_vault_markdown_path(
    vault_root: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
) -> Path:
    """Resolve a Markdown path inside a Vault while blocking traversal/symlink escape."""
    raw = _coerce_relative_path(relative_path)
    relative = PurePosixPath(raw)
    _validate_path_components(raw.split("/"), field_name="Vault path")
    if relative.suffix != ".md":
        raise ValueError("Vault target must have the exact .md extension")

    root = Path(vault_root).resolve(strict=False)
    target = (root / Path(*relative.parts)).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ValueError("Vault path escapes the configured root")
    return target


def validate_memory_attachment_path(
    relative_path: str | os.PathLike[str],
    *,
    memory_id: str | None = None,
) -> str:
    """Validate one canonical content-addressed W5 attachment path."""
    raw = _coerce_relative_path(relative_path)
    relative = PurePosixPath(raw)
    _validate_path_components(raw.split("/"), field_name="attachment path")
    if (
        len(relative.parts) != 4
        or relative.parts[0] != "Memories"
        or relative.parts[2] != "attachments"
        or not _ATTACHMENT_NAME.fullmatch(relative.parts[3])
    ):
        raise ValueError(
            "attachment path must match "
            "Memories/M-id/attachments/Asset-<64hex>.(pdf|txt|html)"
        )
    path_memory_id = validate_memory_id(relative.parts[1])
    if memory_id is not None and path_memory_id != validate_memory_id(memory_id):
        raise ValueError("attachment path must remain inside the selected Memory")
    return relative.as_posix()


def resolve_memory_attachment_path(
    vault_root: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
    *,
    memory_id: str | None = None,
) -> Path:
    """Resolve one W5 attachment while rejecting Vault and Memory escape."""
    raw = validate_memory_attachment_path(relative_path, memory_id=memory_id)
    relative = PurePosixPath(raw)
    root = Path(vault_root).resolve(strict=False)
    lexical_target = root / Path(*relative.parts)
    current = root
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
            raise ValueError("attachment path cannot traverse a symlink or junction")
    memory_root = (root / "Memories" / relative.parts[1]).resolve(strict=False)
    target = lexical_target.resolve(strict=False)
    if not memory_root.is_relative_to(root) or not target.is_relative_to(memory_root):
        raise ValueError("attachment path escapes the selected Memory")
    return target


def build_attachment_wikilink(
    attachment_relative_path: str | os.PathLike[str],
    alias: str | None = None,
) -> str:
    """Build an Obsidian WikiLink whose attachment extension is preserved."""
    target = validate_memory_attachment_path(attachment_relative_path)
    if alias is None:
        return f"[[{target}]]"
    if (
        not isinstance(alias, str)
        or not alias.strip()
        or alias != alias.strip()
        or any(character in alias for character in _INVALID_WIKILINK_CHARACTERS)
    ):
        raise ValueError("attachment WikiLink alias is unsafe")
    return f"[[{target}|{alias}]]"


def validate_wikilink_target(target: str) -> str:
    """Validate one canonical, extension-free Memory note WikiLink target."""
    raw = _coerce_relative_path(target)
    if raw.endswith("/") or any(character in raw for character in _INVALID_WIKILINK_CHARACTERS):
        raise ValueError("WikiLink target contains link syntax or is a directory")
    relative = PurePosixPath(raw)
    _validate_path_components(raw.split("/"), field_name="WikiLink target")
    if len(relative.parts) < 3 or relative.parts[0] != "Memories":
        raise ValueError("WikiLink target must be a Vault-root-relative Memory path")
    validate_memory_id(relative.parts[1])
    if relative.suffix:
        raise ValueError("WikiLink target must omit the Markdown extension")
    return relative.as_posix()


def build_wikilink(
    markdown_relative_path: str | os.PathLike[str],
    alias: str | None = None,
) -> str:
    """Build an unambiguous WikiLink from a canonical Memory Markdown path."""
    raw = _coerce_relative_path(markdown_relative_path)
    if not raw.endswith(".md"):
        raise ValueError("WikiLink source path must have the exact .md extension")
    target = validate_wikilink_target(raw[:-3])
    if alias is None:
        return f"[[{target}]]"
    if (
        not isinstance(alias, str)
        or not alias.strip()
        or alias != alias.strip()
        or any(character in alias for character in _INVALID_WIKILINK_CHARACTERS)
    ):
        raise ValueError("WikiLink alias must be non-empty and cannot contain link syntax")
    return f"[[{target}|{alias}]]"


def detect_legacy_memory_layout(
    vault_root: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Read-only recognition of legacy root reports/evidence/sources directories."""
    root = Path(vault_root).resolve(strict=False)
    present: list[str] = []
    for directory_name in LEGACY_ROOT_DIRECTORIES:
        candidate = root / directory_name
        try:
            resolved = candidate.resolve(strict=False)
            is_safe_directory = resolved.is_relative_to(root) and candidate.is_dir()
        except OSError:
            is_safe_directory = False
        if is_safe_directory:
            present.append(directory_name)
    return tuple(present)


__all__ = [
    "LEGACY_MEMORY_ID",
    "LEGACY_ROOT_DIRECTORIES",
    "build_attachment_wikilink",
    "build_wikilink",
    "detect_legacy_memory_layout",
    "ensure_unique_memory_ids",
    "memory_relative_path",
    "resolve_memory_attachment_path",
    "resolve_vault_markdown_path",
    "validate_frontmatter",
    "validate_memory_descriptor",
    "validate_memory_attachment_path",
    "validate_memory_id",
    "validate_wikilink_target",
]
