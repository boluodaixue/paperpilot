"""Deterministic, rebuildable retrieval over one Markdown Memory."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from .memory import MarkdownMemoryStore
from .vault import memory_relative_path, resolve_vault_markdown_path


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_QUERY_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]+")
_WIKILINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_SUMMARY_MAX_CHARS = 320
_ENGLISH_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "of",
        "on",
        "or",
        "should",
        "than",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
    }
)


@dataclass(frozen=True)
class MemorySearchHit:
    """Bounded metadata returned for one matching Markdown note."""

    relative_path: str
    title: str
    summary: str
    wikilinks: tuple[str, ...]
    score: int
    modified_ns: int
    content_hash: str


@dataclass(frozen=True)
class _IndexedNote:
    hit: MemorySearchHit
    path_text: str
    title_text: str
    frontmatter_text: str
    body: str
    body_text: str


def _split_frontmatter(markdown: str) -> tuple[Mapping[str, object], str]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return {}, markdown
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return {}, markdown
    try:
        loaded = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError:
        return {}, markdown
    frontmatter = loaded if isinstance(loaded, Mapping) else {}
    return frontmatter, "\n".join(lines[closing + 1 :])


def _flatten_scalars(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        flattened: list[str] = []
        for key in sorted(value, key=lambda item: str(item)):
            flattened.append(str(key))
            flattened.extend(_flatten_scalars(value[key]))
        return tuple(flattened)
    if isinstance(value, (list, tuple, set, frozenset)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_scalars(item))
        return tuple(flattened)
    return () if value is None else (str(value),)


def _note_title(frontmatter: Mapping[str, object], body: str, path: Path) -> str:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = _H1.search(body)
    if heading:
        return heading.group(1).strip()
    return path.stem


def _wikilinks(markdown: str) -> tuple[str, ...]:
    targets: list[str] = []
    seen: set[str] = set()
    for match in _WIKILINK.finditer(markdown):
        target = match.group(1).split("|", 1)[0].strip()
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return tuple(targets)


def _summary(body: str, title: str) -> str:
    compact = " ".join(body.split()) or title
    if len(compact) <= _SUMMARY_MAX_CHARS:
        return compact
    return f"{compact[: _SUMMARY_MAX_CHARS - 1].rstrip()}…"


def _matching_summary(body: str, title: str, terms: tuple[str, ...]) -> str:
    compact = " ".join(body.split()) or title
    searchable = compact.casefold()
    positions = [position for term in terms if (position := searchable.find(term)) >= 0]
    if not positions:
        return _summary(body, title)

    start = max(0, min(positions) - 80)
    prefix = "…" if start else ""
    available = _SUMMARY_MAX_CHARS - len(prefix)
    end = min(len(compact), start + available)
    suffix = ""
    if end < len(compact):
        suffix = "…"
        end -= 1
    return f"{prefix}{compact[start:end].strip()}{suffix}"


def _query_terms(query: str) -> tuple[str, ...]:
    terms: set[str] = set()
    for token in _QUERY_TOKEN.findall(query.casefold()):
        is_chinese = any("\u3400" <= character <= "\u9fff" for character in token)
        if not is_chinese and not token.isdigit():
            if len(token) < 3 or token in _ENGLISH_STOPWORDS:
                continue
        terms.add(token)
        if is_chinese:
            terms.update(token[index : index + 2] for index in range(len(token) - 1))
    return tuple(sorted(terms))


class MarkdownMemoryIndex:
    """A disposable in-process index rebuilt from Markdown before every search."""

    def __init__(self, memory_store: MarkdownMemoryStore) -> None:
        self.memory_store = memory_store
        self._notes: dict[str, tuple[_IndexedNote, ...]] = {}

    def _markdown_paths(self, memory_id: str) -> tuple[tuple[str, Path], ...]:
        self.memory_store.get_memory(memory_id)
        relative_root = memory_relative_path(memory_id).rstrip("/")
        memory_root = (self.memory_store.root / relative_root).resolve(strict=False)
        if not memory_root.is_relative_to(self.memory_store.root):
            raise ValueError("Memory path escapes the configured root")

        pending = [memory_root]
        visited: set[Path] = set()
        markdown_paths: list[tuple[str, Path]] = []
        while pending:
            directory = pending.pop()
            resolved_directory = directory.resolve(strict=False)
            if not resolved_directory.is_relative_to(memory_root):
                raise ValueError("Memory entry escapes the selected Memory")
            if resolved_directory in visited:
                continue
            visited.add(resolved_directory)
            for candidate in sorted(directory.iterdir(), key=lambda item: item.name):
                resolved = candidate.resolve(strict=False)
                if not resolved.is_relative_to(memory_root):
                    raise ValueError("Memory entry escapes the selected Memory")
                if candidate.is_dir():
                    pending.append(candidate)
                    continue
                if not candidate.is_file() or candidate.suffix != ".md":
                    continue
                relative = candidate.relative_to(self.memory_store.root).as_posix()
                safe_path = resolve_vault_markdown_path(
                    self.memory_store.root,
                    relative,
                )
                markdown_paths.append((relative, safe_path))
        return tuple(sorted(markdown_paths))

    def rebuild(self, memory_id: str) -> tuple[MemorySearchHit, ...]:
        """Rebuild one Memory index entirely from its current Markdown files."""
        notes: list[_IndexedNote] = []
        for relative_path, path in self._markdown_paths(memory_id):
            with path.open("rb") as handle:
                content = handle.read()
                modified_ns = os.fstat(handle.fileno()).st_mtime_ns
            markdown = content.decode("utf-8")
            frontmatter, body = _split_frontmatter(markdown)
            title = _note_title(frontmatter, body, path)
            heading = _H1.search(body)
            heading_text = heading.group(1).strip() if heading else ""
            hit = MemorySearchHit(
                relative_path=relative_path,
                title=title,
                summary=_summary(body, title),
                wikilinks=_wikilinks(markdown),
                score=0,
                modified_ns=modified_ns,
                content_hash=hashlib.sha256(content).hexdigest(),
            )
            notes.append(
                _IndexedNote(
                    hit=hit,
                    path_text=relative_path.casefold(),
                    title_text=f"{title} {heading_text}".casefold(),
                    frontmatter_text=" ".join(_flatten_scalars(frontmatter)).casefold(),
                    body=body,
                    body_text=body.casefold(),
                )
            )
        ordered = tuple(sorted(notes, key=lambda note: note.hit.relative_path))
        self._notes[memory_id] = ordered
        return tuple(note.hit for note in ordered)

    @staticmethod
    def _text_score(note: _IndexedNote, terms: tuple[str, ...]) -> int:
        score = 0
        for term in terms:
            if term in note.title_text:
                score += 12
            if term in note.path_text:
                score += 9
            if term in note.frontmatter_text:
                score += 7
            if term in note.body_text:
                score += 2
        return score

    @staticmethod
    def _linked_paths(
        note: _IndexedNote,
        notes_by_path: Mapping[str, _IndexedNote],
        notes_by_stem: Mapping[str, tuple[str, ...]],
        memory_id: str,
    ) -> set[str]:
        linked: set[str] = set()
        prefix = f"Memories/{memory_id}/"
        for raw_target in note.hit.wikilinks:
            target = raw_target.split("#", 1)[0].strip()
            if not target or "\\" in target:
                continue
            if target.endswith(".md"):
                markdown_target = target
            else:
                markdown_target = f"{target}.md"
            if markdown_target.startswith("Memories/"):
                candidate = markdown_target
            elif "/" in markdown_target:
                candidate = f"{prefix}{markdown_target}"
            else:
                linked.update(notes_by_stem.get(PurePosixPath(target).stem.casefold(), ()))
                continue
            parts = PurePosixPath(candidate).parts
            if ".." not in parts and candidate.startswith(prefix) and candidate in notes_by_path:
                linked.add(candidate)
        return linked

    def search(
        self,
        memory_id: str,
        query: str,
        limit: int = 5,
    ) -> tuple[MemorySearchHit, ...]:
        """Search current Markdown, then add directly linked notes and backlinks."""
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ValueError("limit must be an integer between 1 and 10")
        self.rebuild(memory_id)
        terms = _query_terms(query)
        if not terms:
            return ()

        notes = self._notes[memory_id]
        notes_by_path = {note.hit.relative_path: note for note in notes}
        stems: dict[str, list[str]] = {}
        for note in notes:
            stems.setdefault(Path(note.hit.relative_path).stem.casefold(), []).append(
                note.hit.relative_path
            )
        notes_by_stem = {key: tuple(sorted(value)) for key, value in stems.items()}
        links = {
            note.hit.relative_path: self._linked_paths(
                note,
                notes_by_path,
                notes_by_stem,
                memory_id,
            )
            for note in notes
        }

        scores = {
            note.hit.relative_path: self._text_score(note, terms)
            for note in notes
        }
        seeds = {path for path, score in scores.items() if score > 0}
        if not seeds:
            return ()
        for seed in seeds:
            neighbors = set(links[seed])
            neighbors.update(path for path, targets in links.items() if seed in targets)
            for neighbor in neighbors:
                scores[neighbor] = max(scores[neighbor], 3)

        results: list[MemorySearchHit] = []
        for path, score in scores.items():
            if score <= 0:
                continue
            note = notes_by_path[path]
            summary = (
                _matching_summary(note.body, note.hit.title, terms)
                if path in seeds
                else note.hit.summary
            )
            results.append(replace(note.hit, score=score, summary=summary))
        results.sort(key=lambda hit: (-hit.score, hit.relative_path.casefold(), hit.relative_path))
        return tuple(results[:limit])


__all__ = ["MarkdownMemoryIndex", "MemorySearchHit"]
