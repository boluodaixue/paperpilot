"""Pure construction of safe Obsidian open URIs for Vault Markdown notes."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from .vault import resolve_memory_attachment_path, resolve_vault_markdown_path


def _query_value(value: str) -> str:
    return quote(value, safe="", encoding="utf-8", errors="strict")


def build_obsidian_open_uri(
    vault_root: str | os.PathLike[str],
    markdown_relative_path: str | os.PathLike[str],
    *,
    vault_name: str | None = None,
) -> str:
    """Build an encoded ``obsidian://open`` URI for one safe Vault note."""
    try:
        target = resolve_vault_markdown_path(vault_root, markdown_relative_path)
    except ValueError as markdown_error:
        try:
            target = resolve_memory_attachment_path(
                vault_root,
                markdown_relative_path,
            )
        except ValueError:
            raise markdown_error
    root = Path(vault_root).resolve(strict=False)

    if vault_name is not None:
        if not isinstance(vault_name, str) or not vault_name.strip():
            raise ValueError("vault_name must be a non-empty string when provided")
        relative = target.relative_to(root).as_posix()
        return (
            f"obsidian://open?vault={_query_value(vault_name)}"
            f"&file={_query_value(relative)}"
        )

    return f"obsidian://open?path={_query_value(target.as_posix())}"


__all__ = ["build_obsidian_open_uri"]
