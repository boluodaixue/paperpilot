"""W2 acceptance tests for pure Obsidian open URI construction."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from src.research import build_obsidian_open_uri


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
        pytest.skip("symbolic links and directory junctions are unavailable")


def test_named_vault_uri_encodes_unicode_spaces_nested_path_and_delimiters(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "论文 Vault"
    vault.mkdir()
    relative = PurePosixPath("Memories/M-attention/reports/研究 & 方法 #1.md")

    uri = build_obsidian_open_uri(vault, relative, vault_name="论文 & Notes #1")

    assert uri == (
        "obsidian://open?"
        "vault=%E8%AE%BA%E6%96%87%20%26%20Notes%20%231&"
        "file=Memories%2FM-attention%2Freports%2F"
        "%E7%A0%94%E7%A9%B6%20%26%20%E6%96%B9%E6%B3%95%20%231.md"
    )
    parsed = urlsplit(uri)
    assert parsed.scheme == "obsidian"
    assert parsed.netloc == "open"
    assert parse_qs(parsed.query) == {
        "vault": ["论文 & Notes #1"],
        "file": [relative.as_posix()],
    }
    assert "+" not in parsed.query


def test_path_uri_uses_encoded_absolute_forward_slash_path(tmp_path: Path) -> None:
    vault = tmp_path / "Vault With Space" / "中文"
    vault.mkdir(parents=True)
    relative = "Memories/M-topic/Home.md"
    target = (vault / "Memories" / "M-topic" / "Home.md").resolve()

    uri = build_obsidian_open_uri(vault, relative)

    parsed = urlsplit(uri)
    assert parsed.scheme == "obsidian"
    assert parsed.netloc == "open"
    assert set(parse_qs(parsed.query)) == {"path"}
    assert parse_qs(parsed.query)["path"] == [target.as_posix()]
    raw_path = parsed.query.removeprefix("path=")
    assert "%2F" in raw_path
    assert "%20" in raw_path
    assert "/" not in raw_path
    assert "\\" not in raw_path
    assert "+" not in raw_path


@pytest.mark.parametrize("vault_name", ["", " ", "\t\r\n"])
def test_blank_vault_name_is_rejected(tmp_path: Path, vault_name: str) -> None:
    with pytest.raises(ValueError, match="vault_name"):
        build_obsidian_open_uri(
            tmp_path,
            "Memories/M-topic/Home.md",
            vault_name=vault_name,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.md",
        "Memories/M-topic/../../outside.md",
        "C:/Vault/Memories/M-topic/Home.md",
        "/Vault/Memories/M-topic/Home.md",
        "Memories/M-topic/Home.txt",
        "Memories/M-topic/Home",
        "Memories/M-topic/Home.MD",
    ],
)
def test_unsafe_or_non_markdown_target_is_rejected(
    tmp_path: Path,
    relative_path: str,
) -> None:
    with pytest.raises(ValueError):
        build_obsidian_open_uri(tmp_path, relative_path, vault_name="Vault")


def test_existing_symlink_escape_is_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / "M-topic"
    memory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Report.md").write_text("outside", encoding="utf-8")
    _make_directory_link(memory / "reports", outside)

    with pytest.raises(ValueError, match="escapes"):
        build_obsidian_open_uri(
            vault,
            "Memories/M-topic/reports/Report.md",
            vault_name="Vault",
        )


def test_nonexistent_target_through_symlink_escape_is_rejected(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    memory = vault / "Memories" / "M-topic"
    memory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(memory / "reports", outside)

    with pytest.raises(ValueError, match="escapes"):
        build_obsidian_open_uri(
            vault,
            "Memories/M-topic/reports/Missing.md",
        )
    assert not (outside / "Missing.md").exists()
