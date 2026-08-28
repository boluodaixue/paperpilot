"""Deterministic, rebuildable retrieval over one Markdown Memory."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Mapping

import yaml

from ..utils.tracing import trace_block, trace_context
from .memory import MarkdownMemoryStore
from .vault import (
    LEGACY_MEMORY_ID,
    LEGACY_ROOT_DIRECTORIES,
    memory_relative_path,
    resolve_vault_markdown_path,
    scan_legacy_memory_markdown,
)


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_QUERY_TOKEN = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]+")
_WIKILINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_SUMMARY_MAX_CHARS = 320
_DEFAULT_INDEX_DB = Path(__file__).resolve().parents[2] / "data" / "retrieval.db"
_CHUNK_MAX_CHARS = 3000
_DEFAULT_RECONCILIATION_SECONDS = 300.0
_INDEX_CONFIGS: dict[str, tuple[Path, float]] = {}
_INDEX_CONFIGS_LOCK = threading.Lock()
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


def _chunks(body: str) -> tuple[str, ...]:
    """Create deterministic bounded chunks without changing Markdown."""
    paragraphs = re.split(r"\n\s*\n", body)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        value = paragraph.strip()
        if not value:
            continue
        while len(value) > _CHUNK_MAX_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(value[:_CHUNK_MAX_CHARS])
            value = value[_CHUNK_MAX_CHARS:]
        candidate = value if not current else f"{current}\n\n{value}"
        if len(candidate) > _CHUNK_MAX_CHARS:
            chunks.append(current)
            current = value
        else:
            current = candidate
    if current:
        chunks.append(current)
    return tuple(chunks or ("",))


def configure_persistent_retrieval(
    memory_store: MarkdownMemoryStore,
    db_path: str | os.PathLike[str],
    *,
    reconciliation_seconds: float = _DEFAULT_RECONCILIATION_SECONDS,
) -> None:
    """Bind product configuration to one Vault without changing Markdown state."""
    if reconciliation_seconds <= 0:
        raise ValueError("retrieval.reconciliation_seconds must be positive")
    key = os.path.normcase(str(memory_store.root.resolve(strict=False)))
    with _INDEX_CONFIGS_LOCK:
        _INDEX_CONFIGS[key] = (
            Path(db_path).resolve(strict=False),
            float(reconciliation_seconds),
        )


class MarkdownMemoryIndex:
    """Persistent FTS5 derivative index; Markdown remains the only truth source."""

    def __init__(
        self,
        memory_store: MarkdownMemoryStore,
        db_path: str | os.PathLike[str] | None = None,
        *,
        reconciliation_seconds: float = _DEFAULT_RECONCILIATION_SECONDS,
    ) -> None:
        self.memory_store = memory_store
        config_key = os.path.normcase(str(self.memory_store.root.resolve(strict=False)))
        with _INDEX_CONFIGS_LOCK:
            configured = _INDEX_CONFIGS.get(config_key)
        if db_path is None and configured is not None:
            db_path, reconciliation_seconds = configured
        self.db_path = Path(db_path) if db_path is not None else _DEFAULT_INDEX_DB
        self.db_path = self.db_path.resolve(strict=False)
        vault_root = self.memory_store.root.resolve(strict=False)
        if self.db_path == vault_root or self.db_path.is_relative_to(vault_root):
            raise ValueError("retrieval index must be stored outside the Markdown Vault")
        if reconciliation_seconds <= 0:
            raise ValueError("reconciliation_seconds must be positive")
        self.reconciliation_seconds = float(reconciliation_seconds)
        canonical_root = os.path.normcase(str(vault_root))
        self.vault_scope = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()
        self._lock = threading.RLock()
        self._notes: dict[str, tuple[_IndexedNote, ...]] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _ensure_tables(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memory_index_documents (
                        vault_scope TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        modified_ns INTEGER NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        title_text TEXT NOT NULL,
                        path_text TEXT NOT NULL,
                        frontmatter_text TEXT NOT NULL,
                        body TEXT NOT NULL,
                        wikilinks_json TEXT NOT NULL,
                        PRIMARY KEY (vault_scope, memory_id, relative_path)
                    );
                    CREATE TABLE IF NOT EXISTS memory_index_state (
                        vault_scope TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        last_full_reconcile REAL NOT NULL,
                        PRIMARY KEY (vault_scope, memory_id)
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
                        vault_scope UNINDEXED,
                        memory_id UNINDEXED,
                        relative_path UNINDEXED,
                        chunk_id UNINDEXED,
                        content_hash UNINDEXED,
                        title,
                        path_text,
                        frontmatter,
                        body,
                        terms,
                        wikilinks,
                        tokenize = 'unicode61 remove_diacritics 2'
                    );
                    """
                )
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                if "fts5" in str(exc).casefold():
                    raise RuntimeError("SQLite FTS5 is required for persistent retrieval") from exc
                raise
            finally:
                connection.close()

    def _markdown_paths(self, memory_id: str) -> tuple[tuple[str, Path], ...]:
        if memory_id == LEGACY_MEMORY_ID:
            return scan_legacy_memory_markdown(self.memory_store.root)
        self.memory_store.get_memory(memory_id)
        relative_root = memory_relative_path(memory_id).rstrip("/")
        lexical_root = self.memory_store.root / relative_root
        memory_root = lexical_root.resolve(strict=False)
        if not memory_root.is_relative_to(self.memory_store.root):
            raise ValueError("Memory path escapes the configured root")
        if lexical_root != memory_root or lexical_root.is_symlink() or bool(
            getattr(lexical_root.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Memory root escapes its canonical path through a symlink or junction")

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
                if candidate.is_symlink() or bool(
                    getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400
                ):
                    raise ValueError("Memory entry escapes through a symlink or junction")
                resolved = candidate.resolve(strict=False)
                if not resolved.is_relative_to(memory_root):
                    raise ValueError("Memory entry escapes the selected Memory")
                if candidate.is_dir():
                    pending.append(candidate)
                    continue
                if not candidate.is_file() or candidate.suffix != ".md":
                    continue
                relative = candidate.relative_to(self.memory_store.root).as_posix()
                resolve_vault_markdown_path(
                    self.memory_store.root,
                    relative,
                )
                markdown_paths.append((relative, candidate))
        return tuple(sorted(markdown_paths))

    @staticmethod
    def _indexed_note(row: Mapping[str, object]) -> _IndexedNote:
        links = json.loads(str(row["wikilinks_json"]))
        if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
            raise ValueError("persistent retrieval WikiLinks are invalid")
        title = str(row["title"])
        body = str(row["body"])
        hit = MemorySearchHit(
            relative_path=str(row["relative_path"]),
            title=title,
            summary=_summary(body, title),
            wikilinks=tuple(links),
            score=0,
            modified_ns=int(row["modified_ns"]),
            content_hash=str(row["content_hash"]),
        )
        return _IndexedNote(
            hit=hit,
            path_text=str(row["path_text"]),
            title_text=str(row["title_text"]),
            frontmatter_text=str(row["frontmatter_text"]),
            body=body,
            body_text=body.casefold(),
        )

    def _load_notes(self, connection: sqlite3.Connection, memory_id: str) -> tuple[_IndexedNote, ...]:
        rows = connection.execute(
            "SELECT * FROM memory_index_documents WHERE vault_scope = ? AND memory_id = ? "
            "ORDER BY relative_path",
            (self.vault_scope, memory_id),
        ).fetchall()
        notes = tuple(self._indexed_note(dict(row)) for row in rows)
        self._notes[memory_id] = notes
        return notes

    def sync(
        self,
        memory_id: str,
        *,
        force_hash: bool = False,
    ) -> tuple[MemorySearchHit, ...]:
        """Incrementally converge one Memory from current safe Markdown files."""
        paths = self._markdown_paths(memory_id)
        with self._lock:
            connection = self._connect()
            try:
                existing_rows = connection.execute(
                    "SELECT relative_path, content_hash, modified_ns, size_bytes "
                    "FROM memory_index_documents WHERE vault_scope = ? AND memory_id = ?",
                    (self.vault_scope, memory_id),
                ).fetchall()
                existing = {str(row["relative_path"]): row for row in existing_rows}
                state = connection.execute(
                    "SELECT last_full_reconcile FROM memory_index_state "
                    "WHERE vault_scope = ? AND memory_id = ?",
                    (self.vault_scope, memory_id),
                ).fetchone()
                now = time.time()
                full = force_hash or state is None or (
                    now - float(state["last_full_reconcile"])
                    >= self.reconciliation_seconds
                )
                updates: list[dict[str, object]] = []
                current_paths: set[str] = set()
                for relative_path, path in paths:
                    current_paths.add(relative_path)
                    stat = path.stat()
                    old = existing.get(relative_path)
                    if (
                        not full
                        and old is not None
                        and int(old["modified_ns"]) == stat.st_mtime_ns
                        and int(old["size_bytes"]) == stat.st_size
                    ):
                        continue
                    with path.open("rb") as handle:
                        content = handle.read()
                        opened = os.fstat(handle.fileno())
                    if opened.st_mtime_ns != stat.st_mtime_ns or opened.st_size != stat.st_size:
                        raise ValueError(f"Markdown changed while indexing: {relative_path}")
                    content_hash = hashlib.sha256(content).hexdigest()
                    if old is not None and str(old["content_hash"]) == content_hash:
                        updates.append(
                            {
                                "relative_path": relative_path,
                                "metadata_only": True,
                                "modified_ns": opened.st_mtime_ns,
                                "size_bytes": opened.st_size,
                            }
                        )
                        continue
                    markdown = content.decode("utf-8")
                    frontmatter, body = _split_frontmatter(markdown)
                    title = _note_title(frontmatter, body, path)
                    heading = _H1.search(body)
                    heading_text = heading.group(1).strip() if heading else ""
                    updates.append(
                        {
                            "relative_path": relative_path,
                            "metadata_only": False,
                            "content_hash": content_hash,
                            "modified_ns": opened.st_mtime_ns,
                            "size_bytes": opened.st_size,
                            "title": title,
                            "title_text": f"{title} {heading_text}".casefold(),
                            "path_text": relative_path.casefold(),
                            "frontmatter_text": " ".join(_flatten_scalars(frontmatter)).casefold(),
                            "body": body,
                            "wikilinks": _wikilinks(markdown),
                        }
                    )
                deleted = set(existing) - current_paths
                connection.execute("BEGIN IMMEDIATE")
                for relative_path in sorted(deleted):
                    connection.execute(
                        "DELETE FROM memory_index_documents WHERE vault_scope = ? AND memory_id = ? AND relative_path = ?",
                        (self.vault_scope, memory_id, relative_path),
                    )
                    connection.execute(
                        "DELETE FROM memory_chunks_fts WHERE vault_scope = ? AND memory_id = ? AND relative_path = ?",
                        (self.vault_scope, memory_id, relative_path),
                    )
                for value in updates:
                    relative_path = str(value["relative_path"])
                    if value["metadata_only"]:
                        connection.execute(
                            "UPDATE memory_index_documents SET modified_ns = ?, size_bytes = ? "
                            "WHERE vault_scope = ? AND memory_id = ? AND relative_path = ?",
                            (
                                value["modified_ns"],
                                value["size_bytes"],
                                self.vault_scope,
                                memory_id,
                                relative_path,
                            ),
                        )
                        continue
                    connection.execute(
                        "DELETE FROM memory_chunks_fts WHERE vault_scope = ? AND memory_id = ? AND relative_path = ?",
                        (self.vault_scope, memory_id, relative_path),
                    )
                    connection.execute(
                        "INSERT INTO memory_index_documents "
                        "(vault_scope, memory_id, relative_path, content_hash, modified_ns, size_bytes, title, title_text, path_text, frontmatter_text, body, wikilinks_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(vault_scope, memory_id, relative_path) DO UPDATE SET "
                        "content_hash=excluded.content_hash, modified_ns=excluded.modified_ns, size_bytes=excluded.size_bytes, "
                        "title=excluded.title, title_text=excluded.title_text, path_text=excluded.path_text, "
                        "frontmatter_text=excluded.frontmatter_text, body=excluded.body, wikilinks_json=excluded.wikilinks_json",
                        (
                            self.vault_scope,
                            memory_id,
                            relative_path,
                            value["content_hash"],
                            value["modified_ns"],
                            value["size_bytes"],
                            value["title"],
                            value["title_text"],
                            value["path_text"],
                            value["frontmatter_text"],
                            value["body"],
                            json.dumps(value["wikilinks"], ensure_ascii=False),
                        ),
                    )
                    links_text = " ".join(value["wikilinks"])
                    for index, chunk in enumerate(_chunks(str(value["body"]))):
                        terms = " ".join(
                            _query_terms(
                                " ".join(
                                    (
                                        str(value["title"]),
                                        relative_path,
                                        str(value["frontmatter_text"]),
                                        chunk,
                                    )
                                )
                            )
                        )
                        connection.execute(
                            "INSERT INTO memory_chunks_fts "
                            "(vault_scope, memory_id, relative_path, chunk_id, content_hash, title, path_text, frontmatter, body, terms, wikilinks) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                self.vault_scope,
                                memory_id,
                                relative_path,
                                f"{relative_path}#{index}",
                                value["content_hash"],
                                value["title"],
                                value["path_text"],
                                value["frontmatter_text"],
                                chunk,
                                terms,
                                links_text,
                            ),
                        )
                if full:
                    connection.execute(
                        "INSERT INTO memory_index_state (vault_scope, memory_id, last_full_reconcile) VALUES (?, ?, ?) "
                        "ON CONFLICT(vault_scope, memory_id) DO UPDATE SET last_full_reconcile=excluded.last_full_reconcile",
                        (self.vault_scope, memory_id, now),
                    )
                connection.commit()
                notes = self._load_notes(connection, memory_id)
                return tuple(note.hit for note in notes)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def rebuild(self, memory_id: str) -> tuple[MemorySearchHit, ...]:
        """Delete one derived scope and rebuild it only from current Markdown."""
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM memory_chunks_fts WHERE vault_scope = ? AND memory_id = ?",
                    (self.vault_scope, memory_id),
                )
                connection.execute(
                    "DELETE FROM memory_index_documents WHERE vault_scope = ? AND memory_id = ?",
                    (self.vault_scope, memory_id),
                )
                connection.execute(
                    "DELETE FROM memory_index_state WHERE vault_scope = ? AND memory_id = ?",
                    (self.vault_scope, memory_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.sync(memory_id, force_hash=True)

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
            if memory_id == LEGACY_MEMORY_ID:
                if "/" not in markdown_target:
                    linked.update(
                        notes_by_stem.get(PurePosixPath(target).stem.casefold(), ())
                    )
                    continue
                parts = PurePosixPath(markdown_target).parts
                if (
                    ".." not in parts
                    and parts
                    and parts[0] in LEGACY_ROOT_DIRECTORIES
                    and markdown_target in notes_by_path
                ):
                    linked.add(markdown_target)
                continue
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

    @staticmethod
    def _fts_expression(terms: tuple[str, ...]) -> str:
        return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)

    def _rank(self, memory_id: str, terms: tuple[str, ...], limit: int) -> tuple[MemorySearchHit, ...]:
        notes = self._notes[memory_id]
        notes_by_path = {note.hit.relative_path: note for note in notes}
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT relative_path, bm25(memory_chunks_fts, 0, 0, 0, 0, 12, 9, 7, 2, 1, 1) AS rank "
                    "FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ? "
                    "AND vault_scope = ? AND memory_id = ? ORDER BY rank LIMIT ?",
                    (
                        self._fts_expression(terms),
                        self.vault_scope,
                        memory_id,
                        max(50, limit * 20),
                    ),
                ).fetchall()
            finally:
                connection.close()
        candidate_paths = {str(row["relative_path"]) for row in rows}
        stems: dict[str, list[str]] = {}
        for note in notes:
            stems.setdefault(Path(note.hit.relative_path).stem.casefold(), []).append(note.hit.relative_path)
        notes_by_stem = {key: tuple(sorted(value)) for key, value in stems.items()}
        links = {
            note.hit.relative_path: self._linked_paths(note, notes_by_path, notes_by_stem, memory_id)
            for note in notes
        }
        scores = {path: 0 for path in notes_by_path}
        for path in candidate_paths:
            note = notes_by_path.get(path)
            if note is not None:
                scores[path] = self._text_score(note, terms)
        seeds = {path for path, score in scores.items() if score > 0}
        for seed in seeds:
            neighbors = set(links[seed])
            neighbors.update(path for path, targets in links.items() if seed in targets)
            for neighbor in neighbors:
                scores[neighbor] = max(scores[neighbor], 3)
        ranked: list[MemorySearchHit] = []
        for path, score in scores.items():
            if score <= 0:
                continue
            note = notes_by_path[path]
            summary = _matching_summary(note.body, note.hit.title, terms) if path in seeds else note.hit.summary
            ranked.append(replace(note.hit, score=score, summary=summary))
        ranked.sort(key=lambda hit: (-hit.score, hit.relative_path.casefold(), hit.relative_path))
        return tuple(ranked[:limit])

    def _valid_hit(self, memory_id: str, hit: MemorySearchHit) -> bool:
        if memory_id == LEGACY_MEMORY_ID:
            allowed = {relative: path for relative, path in scan_legacy_memory_markdown(self.memory_store.root)}
        else:
            try:
                allowed = dict(self._markdown_paths(memory_id))
            except (OSError, ValueError):
                return False
        path = allowed.get(hit.relative_path)
        if path is None:
            return False
        try:
            with path.open("rb") as handle:
                content = handle.read()
        except OSError:
            return False
        return hashlib.sha256(content).hexdigest() == hit.content_hash

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
        trace_metadata = {"memory_id": memory_id, "limit": limit}
        with trace_context(
            trace_name="paperpilot.memory.retrieval",
            tags=["paperpilot", "memory", "retrieval"],
            metadata=trace_metadata,
        ):
            with trace_block(
                "memory.search",
                run_type="retriever",
                inputs=trace_metadata,
                tags=["paperpilot", "memory", "retrieval"],
            ) as observation:
                self.sync(memory_id)
                terms = _query_terms(query)
                results: tuple[MemorySearchHit, ...] = ()
                if terms:
                    results = self._rank(memory_id, terms, limit)
                    if any(not self._valid_hit(memory_id, hit) for hit in results):
                        self.sync(memory_id, force_hash=True)
                        results = tuple(
                            hit
                            for hit in self._rank(memory_id, terms, limit)
                            if self._valid_hit(memory_id, hit)
                        )[:limit]
                observation.add_output(
                    {
                        "memory_id": memory_id,
                        "query_term_count": len(terms),
                        "hit_count": len(results),
                        "retrieved_files": [
                            {"path": hit.relative_path, "score": hit.score}
                            for hit in results
                        ],
                    }
                )
                return results


__all__ = ["MarkdownMemoryIndex", "MemorySearchHit", "configure_persistent_retrieval"]
