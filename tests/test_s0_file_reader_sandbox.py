"""S0 acceptance tests for the context-bound local file reader sandbox."""
from __future__ import annotations

import asyncio
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

import src.tools.file_reader as file_reader_module
from src.tools import FileReaderError, FileReaderTool, file_reader_scope


def _write(root: Path, relative: str, content: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError) as symlink_error:
        if os.name != "nt":
            pytest.skip(f"symbolic links are unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("neither symbolic links nor junctions are available")


def test_tool_is_deny_all_without_a_scope_and_schema_is_contextual() -> None:
    tool = FileReaderTool()

    assert tool.is_available() is False
    assert tool.get_openai_tool_schema()["function"]["parameters"]["properties"][
        "root"
    ]["enum"] == []

    with pytest.raises(FileReaderError, match="not authorized"):
        asyncio.run(tool.execute(root="memory", path="note.md"))


@pytest.mark.asyncio
async def test_reads_memory_and_upload_as_structured_virtual_paths(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    upload = tmp_path / "upload"
    _write(memory, "notes/N-safe.md", "\ufeffMemory text".encode("utf-8"))
    _write(upload, "dataset.csv", b"country,value\nUS,30\nCN,6\n")
    tool = FileReaderTool()

    with file_reader_scope({"memory": memory, "upload": upload}):
        assert tool.is_available() is True
        schema = tool.get_openai_tool_schema()
        assert schema["function"]["parameters"]["properties"]["root"]["enum"] == [
            "memory",
            "upload",
        ]
        note = await tool.execute(root="memory", path="notes/N-safe.md")
        dataset = await tool.execute(root="upload", path="dataset.csv")

    assert note == {
        "path": "memory/notes/N-safe.md",
        "format": "markdown",
        "content": "Memory text",
        "truncated": False,
    }
    assert dataset["path"] == "upload/dataset.csv"
    assert dataset["format"] == "csv"
    assert "US,30" in dataset["content"]
    assert tool.is_available() is False


@pytest.mark.asyncio
async def test_scope_copies_roots_and_resets_nested_scopes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "value.txt", b"first")
    _write(second, "value.txt", b"second")
    mutable_roots = {"memory": first}
    tool = FileReaderTool()

    with file_reader_scope(mutable_roots):
        mutable_roots["memory"] = second
        assert (await tool.execute(root="memory", path="value.txt"))["content"] == "first"
        with file_reader_scope(None):
            assert tool.is_available() is False
        assert (await tool.execute(root="memory", path="value.txt"))["content"] == "first"
    assert tool.is_available() is False


@pytest.mark.asyncio
async def test_context_scopes_are_isolated_across_async_tasks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "value.txt", b"first")
    _write(second, "value.txt", b"second")
    tool = FileReaderTool()
    ready = [asyncio.Event(), asyncio.Event()]
    proceed = asyncio.Event()

    async def scoped_read(index: int, root: Path) -> str:
        with file_reader_scope({"memory": root}):
            ready[index].set()
            await proceed.wait()
            return str((await tool.execute(root="memory", path="value.txt"))["content"])

    tasks = [
        asyncio.create_task(scoped_read(0, first)),
        asyncio.create_task(scoped_read(1, second)),
    ]
    await asyncio.gather(*(event.wait() for event in ready))
    proceed.set()

    assert await asyncio.gather(*tasks) == ["first", "second"]
    assert tool.is_available() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "../outside.txt",
        "notes/../../outside.txt",
        "/absolute.txt",
        "C:/absolute.txt",
        "//server/share.txt",
        "notes\\note.txt",
        "notes/./note.txt",
        "notes//note.txt",
        "notes/NUL.txt",
        "notes/note.txt:stream",
        "notes/note.txt.",
        "notes/note.txt ",
    ],
)
async def test_rejects_unsafe_or_absolute_paths(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    tool = FileReaderTool()

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError):
            await tool.execute(root="memory", path=unsafe)


@pytest.mark.asyncio
async def test_neighbor_prefix_and_other_named_root_are_not_authorized(tmp_path: Path) -> None:
    root = tmp_path / "M-safe"
    neighbor = tmp_path / "M-safe-copy"
    root.mkdir()
    _write(neighbor, "secret.txt", b"secret")
    tool = FileReaderTool()

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError, match="not authorized"):
            await tool.execute(root="neighbor", path="secret.txt")
        with pytest.raises(FileReaderError, match="absolute"):
            await tool.execute(root="memory", path=neighbor.as_posix())


@pytest.mark.asyncio
async def test_rejects_directory_link_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    _write(outside, "secret.txt", b"secret")
    _make_directory_link(root / "linked", outside)
    tool = FileReaderTool()

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError, match="linked paths"):
            await tool.execute(root="memory", path="linked/secret.txt")


@pytest.mark.asyncio
async def test_rejects_final_file_symlink_even_when_it_points_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    target = _write(root, "target.txt", b"inside")
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")
    tool = FileReaderTool()

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError, match="linked paths"):
            await tool.execute(root="memory", path="link.txt")


@pytest.mark.asyncio
async def test_rejects_authorized_root_replaced_with_a_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    original = tmp_path / "original-root"
    outside = tmp_path / "outside"
    _write(root, "note.txt", b"authorized")
    _write(outside, "note.txt", b"outside")
    tool = FileReaderTool()

    with file_reader_scope({"memory": root}):
        root.rename(original)
        _make_directory_link(root, outside)
        with pytest.raises(FileReaderError, match="authorized root changed"):
            await tool.execute(root="memory", path="note.txt")


@pytest.mark.asyncio
async def test_rejects_toctou_replacement_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    target = _write(root, "note.txt", b"authorized")
    replacement = _write(root, "replacement.tmp", b"replacement")
    original_open = file_reader_module.os.open
    swapped = False

    def swapping_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(path) == target:
            swapped = True
            os.replace(replacement, target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(file_reader_module.os, "open", swapping_open)
    tool = FileReaderTool()
    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError, match="changed while being authorized"):
            await tool.execute(root="memory", path="note.txt")


@pytest.mark.asyncio
async def test_size_extension_utf8_and_output_limits_are_enforced(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(root, "large.txt", b"123456")
    _write(root, "binary.txt", b"\xff\xfe")
    _write(root, "script.py", b"print('unsafe')")

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError, match="size limit"):
            await FileReaderTool(max_file_size=5).execute(
                root="memory", path="large.txt"
            )
        with pytest.raises(FileReaderError, match="valid UTF-8"):
            await FileReaderTool().execute(root="memory", path="binary.txt")
        with pytest.raises(FileReaderError, match="unsupported"):
            await FileReaderTool().execute(root="memory", path="script.py")
        result = await FileReaderTool(max_output_chars=3).execute(
            root="memory", path="large.txt"
        )

    assert result["content"] == "123"
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_csv_row_and_cell_limits_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write(root, "data.csv", ("name,value\n" + "x," + "a" * 3_000 + "\ny,2\n").encode())

    with file_reader_scope({"upload": root}):
        result = await FileReaderTool(max_csv_rows=2, max_output_chars=5_000).execute(
            root="upload", path="data.csv"
        )

    assert result["truncated"] is True
    assert len(result["content"]) < 5_000
    assert "y,2" not in result["content"]


@pytest.mark.asyncio
async def test_docx_zip_bomb_limit_is_checked_before_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    target = root / "bomb.docx"
    root.mkdir()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 100)
    monkeypatch.setattr(file_reader_module, "_MAX_DOCX_UNCOMPRESSED_BYTES", 50)

    with file_reader_scope({"upload": root}):
        with pytest.raises(FileReaderError, match="expands beyond"):
            await FileReaderTool().execute(root="upload", path="bomb.docx")


@pytest.mark.asyncio
async def test_pdf_is_parsed_from_the_validated_byte_snapshot(tmp_path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    root = tmp_path / "root"
    target = root / "blank.pdf"
    root.mkdir()
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with target.open("wb") as handle:
        writer.write(handle)

    with file_reader_scope({"memory": root}):
        result = await FileReaderTool().execute(root="memory", path="blank.pdf")

    assert result == {
        "path": "memory/blank.pdf",
        "format": "pdf",
        "content": "",
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_errors_never_disclose_host_paths(tmp_path: Path) -> None:
    root = tmp_path / "private-host-directory"
    root.mkdir()

    with file_reader_scope({"memory": root}):
        with pytest.raises(FileReaderError) as captured:
            await FileReaderTool().execute(root="memory", path="missing.txt")

    message = str(captured.value)
    assert "memory/missing.txt" in message
    assert str(root) not in message
    assert str(tmp_path) not in message


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_size": 0},
        {"max_output_chars": -1},
        {"max_pdf_pages": True},
        {"max_csv_rows": 0},
        {"max_docx_paragraphs": 0},
    ],
)
def test_constructor_rejects_invalid_limits(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        FileReaderTool(**kwargs)  # type: ignore[arg-type]
