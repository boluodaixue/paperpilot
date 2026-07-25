"""W0 acceptance tests for the Memory/Vault safety contract."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

from src.research.models import MemoryDescriptor
from src.research.vault import (
    LEGACY_MEMORY_ID,
    build_wikilink,
    detect_legacy_memory_layout,
    ensure_unique_memory_ids,
    memory_relative_path,
    resolve_vault_markdown_path,
    validate_frontmatter,
    validate_memory_descriptor,
    validate_memory_id,
    validate_wikilink_target,
)


CREATED_AT = "2026-08-28T12:00:00+08:00"
UPDATED_AT = "2026-08-28T12:30:00+08:00"


def _descriptor(
    memory_id: str = "M-transformer-evidence",
    *,
    title: str = "Transformer Evidence",
) -> MemoryDescriptor:
    return MemoryDescriptor(
        memory_id=memory_id,
        title=title,
        relative_path=memory_relative_path(memory_id),
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _frontmatter() -> dict[str, object]:
    return {
        "id": "N-transformer-summary",
        "type": "note",
        "memory_id": "M-transformer-evidence",
        "title": "Transformer summary",
        "created_at": CREATED_AT,
        "updated_at": UPDATED_AT,
        "origin": "user",
        "status": "confirmed",
        "tags": ["paperpilot", "transformers"],
    }


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "directory", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return tuple(snapshot)


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError) as symlink_error:
        if os.name != "nt":
            pytest.skip(f"symbolic links are unavailable: {symlink_error}")

    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if junction.returncode != 0:
        pytest.skip(
            "neither symbolic links nor directory junctions are available: "
            f"{junction.stderr or junction.stdout}"
        )


def test_memory_descriptor_uses_stable_id_and_canonical_relative_path() -> None:
    descriptor = _descriptor()

    assert validate_memory_id(descriptor.memory_id) == "M-transformer-evidence"
    assert descriptor.relative_path == "Memories/M-transformer-evidence/"
    assert validate_memory_descriptor(descriptor) == descriptor


def test_title_change_does_not_change_memory_id_or_relative_path() -> None:
    original = _descriptor()
    renamed = replace(original, title="Renamed by the user")

    assert validate_memory_descriptor(renamed) == renamed
    assert renamed.memory_id == original.memory_id
    assert renamed.relative_path == original.relative_path


@pytest.mark.parametrize(
    "memory_id",
    [
        "transformer-evidence",
        "M-",
        "M-has spaces",
        "M-../escape",
        "M-under_score",
        "M-中文",
    ],
)
def test_memory_id_rejects_noncanonical_values(memory_id: str) -> None:
    with pytest.raises(ValueError):
        validate_memory_id(memory_id)
    with pytest.raises(ValueError):
        memory_relative_path(memory_id)


def test_memory_descriptor_rejects_noncanonical_relative_path() -> None:
    descriptor = replace(
        _descriptor(),
        relative_path="Memories/M-transformer-evidence-renamed/",
    )

    with pytest.raises(ValueError):
        validate_memory_descriptor(descriptor)


def test_duplicate_memory_ids_are_rejected_for_descriptors_and_strings() -> None:
    descriptor = _descriptor()

    with pytest.raises(ValueError):
        ensure_unique_memory_ids((descriptor, replace(descriptor, title="Duplicate")))
    with pytest.raises(ValueError):
        ensure_unique_memory_ids(("M-alpha", "M-beta", "M-alpha"))

    assert ensure_unique_memory_ids(("M-alpha", _descriptor("M-beta"))) == (
        "M-alpha",
        "M-beta",
    )


def test_valid_flat_frontmatter_is_accepted() -> None:
    frontmatter = _frontmatter()

    assert validate_frontmatter(frontmatter) == frontmatter


@pytest.mark.parametrize(
    "missing_field",
    [
        "id",
        "type",
        "memory_id",
        "title",
        "created_at",
        "updated_at",
        "origin",
        "status",
        "tags",
    ],
)
def test_frontmatter_rejects_each_missing_required_field(missing_field: str) -> None:
    frontmatter = _frontmatter()
    del frontmatter[missing_field]

    with pytest.raises(ValueError):
        validate_frontmatter(frontmatter)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("id", "../N-escape"),
        ("id", "N-has spaces"),
        ("memory_id", "not-prefixed"),
        ("memory_id", "M-../escape"),
        ("type", "../note"),
        ("type", ""),
        ("created_at", "2026-08-28T12:00:00"),
        ("updated_at", "2026-08-28T12:30:00"),
        ("created_at", "not-an-iso-timestamp"),
        ("updated_at", "2026-99-99T99:99:99+08:00"),
        ("tags", "paperpilot"),
        ("tags", ["paperpilot", 1]),
        ("tags", [{"nested": "value"}]),
    ],
)
def test_frontmatter_rejects_invalid_ids_timestamps_and_tags(
    field: str,
    invalid_value: object,
) -> None:
    frontmatter = {**_frontmatter(), field: invalid_value}

    with pytest.raises(ValueError):
        validate_frontmatter(frontmatter)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("extra", {"nested": "value"}),
        ("title", ["not", "flat"]),
    ],
)
def test_frontmatter_rejects_nested_or_non_tag_sequence_values(
    field: str,
    invalid_value: object,
) -> None:
    frontmatter = {**_frontmatter(), field: invalid_value}

    with pytest.raises(ValueError):
        validate_frontmatter(frontmatter)


def test_vault_markdown_path_accepts_existing_and_new_paths_inside_vault(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    existing = vault / "Memories" / "M-safe" / "notes" / "N-existing.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")

    assert resolve_vault_markdown_path(
        vault,
        "Memories/M-safe/notes/N-existing.md",
    ) == existing.resolve()
    assert resolve_vault_markdown_path(
        vault,
        "Memories/M-safe/notes/N-new.md",
    ) == (existing.parent / "N-new.md").resolve()


def test_vault_markdown_path_rejects_absolute_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = (tmp_path / "outside.md").resolve()

    with pytest.raises(ValueError):
        resolve_vault_markdown_path(vault, outside)


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.md",
        "Memories/M-safe/../../outside.md",
        "Memories/M-safe/notes/../../../outside.md",
    ],
)
def test_vault_markdown_path_rejects_parent_traversal(
    tmp_path: Path,
    relative_path: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(ValueError):
        resolve_vault_markdown_path(vault, relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "Memories/M-safe/notes/readme.txt",
        "Memories/M-safe/notes/no-extension",
        "Memories/M-safe/notes/archive.md.zip",
    ],
)
def test_vault_markdown_path_rejects_non_markdown_targets(
    tmp_path: Path,
    relative_path: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(ValueError):
        resolve_vault_markdown_path(vault, relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "Memories/M-safe/notes/CON.md",
        "Memories/M-safe/notes/N-note.md:stream",
        "Memories/M-safe/notes/N-note\t.md",
    ],
)
def test_vault_markdown_path_rejects_unsafe_windows_components(
    tmp_path: Path,
    relative_path: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(ValueError):
        resolve_vault_markdown_path(vault, relative_path)


def test_vault_markdown_path_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / "M-safe"
    memory.mkdir(parents=True)
    outside_directory = tmp_path / "outside-existing"
    outside_directory.mkdir()
    (outside_directory / "N-linked.md").write_text("outside", encoding="utf-8")
    link = memory / "notes"
    _make_directory_link(link, outside_directory)

    with pytest.raises(ValueError):
        resolve_vault_markdown_path(vault, "Memories/M-safe/notes/N-linked.md")


def test_vault_markdown_path_rejects_nonexistent_target_through_symlink_escape(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / "M-safe"
    memory.mkdir(parents=True)
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    link = memory / "notes"
    _make_directory_link(link, outside_directory)

    escaped_target = outside_directory / "N-not-created.md"
    assert not escaped_target.exists()
    with pytest.raises(ValueError):
        resolve_vault_markdown_path(
            vault,
            "Memories/M-safe/notes/N-not-created.md",
        )
    assert not escaped_target.exists()


def test_wikilink_target_is_vault_relative_explicit_and_extensionless() -> None:
    target = "Memories/M-transformer-evidence/evidence/E-attention"

    assert validate_wikilink_target(target) == target
    assert build_wikilink(f"{target}.md") == f"[[{target}]]"
    assert build_wikilink(f"{target}.md", alias="Attention evidence") == (
        f"[[{target}|Attention evidence]]"
    )


@pytest.mark.parametrize(
    "target",
    [
        "E-attention",
        "/Memories/M-transformer-evidence/evidence/E-attention",
        "C:/Vault/Memories/M-transformer-evidence/evidence/E-attention",
        "Memories/M-transformer-evidence/../M-other/notes/N-note",
        "Memories/M-transformer-evidence/evidence/E-attention.md",
        "reports/Report-old",
        "Inbox/N-note",
    ],
)
def test_wikilink_target_rejects_ambiguous_unsafe_or_non_memory_targets(
    target: str,
) -> None:
    with pytest.raises(ValueError):
        validate_wikilink_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "Memories/M-transformer-evidence/notes/NUL",
        "Memories/M-transformer-evidence/notes/N-note:stream",
        "Memories/M-transformer-evidence/notes/N-note.",
    ],
)
def test_wikilink_target_rejects_unsafe_windows_components(target: str) -> None:
    with pytest.raises(ValueError):
        validate_wikilink_target(target)


def test_legacy_root_layout_is_detected_read_only(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    contents = {
        "reports/Report-old.md": "# Existing report\n",
        "evidence/Evidence-old.md": "# Existing evidence\n",
        "sources/Source-old.md": "# Existing source\n",
        "Inbox/User-note.md": "# User note\n",
    }
    for relative_path, content in contents.items():
        path = vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    before = _tree_snapshot(vault)

    detected = detect_legacy_memory_layout(vault)

    assert LEGACY_MEMORY_ID == "M-legacy"
    assert detected == ("reports", "evidence", "sources")
    assert _tree_snapshot(vault) == before
    assert detect_legacy_memory_layout(vault) == detected
    assert _tree_snapshot(vault) == before


def test_legacy_layout_ignores_missing_entries_and_same_named_files(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "evidence").mkdir(parents=True)
    (vault / "reports").write_text("not a directory", encoding="utf-8")

    before = _tree_snapshot(vault)
    assert detect_legacy_memory_layout(vault) == ("evidence",)
    assert _tree_snapshot(vault) == before
