"""User-triggered, evidence-grounded Wiki page generation and validation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

import yaml

from .memory import MarkdownMemoryStore, MemoryWriteConflictError
from .models import WikiClaim, WikiDraft, WikiSection
from .policy import call_policy
from .vault import build_wikilink, memory_relative_path, validate_frontmatter, validate_memory_id

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)
_WIKILINK = re.compile(r"\[\[([^\]|\r\n]+)(?:\|[^\]\r\n]*)?\]\]")
_SAFE_WIKI_ID = re.compile(r"^Wiki-[0-9a-f]{32}$")
_MAX_REPORT_CHARS = 30_000
_MAX_EXISTING_PAGE_CHARS = 24_000
_MAX_EVIDENCE_NOTES = 24
_MAX_EVIDENCE_CHARS = 4_000


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _frontmatter(markdown: str, *, label: str) -> dict[str, Any]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{label} must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{label} frontmatter is not closed") from exc
    try:
        payload = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise ValueError(f"{label} frontmatter is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} frontmatter must be a mapping")
    return payload


def _managed_path(memory_id: str, category: str, path: str) -> str:
    validate_memory_id(memory_id)
    raw = str(path or "")
    if "\\" in raw:
        raise ValueError("managed paths must use forward slashes")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or pure.as_posix() != raw
        or len(pure.parts) != 4
        or pure.parts[:3] != ("Memories", memory_id, category)
        or pure.suffix.lower() != ".md"
    ):
        raise ValueError(f"path must belong to the selected Memory {category} directory")
    return raw


def _wiki_path(memory_id: str, path: str, *, allow_index: bool = False) -> str:
    normalized = _managed_path(memory_id, "wiki", path)
    if not allow_index and PurePosixPath(normalized).name.casefold() == "index.md":
        raise ValueError("Wiki Index.md cannot be used as a topic page")
    return normalized


def _report_path(memory_id: str, path: str) -> str:
    return _managed_path(memory_id, "reports", path)


def _evidence_paths(memory_id: str, markdown: str) -> tuple[str, ...]:
    prefix = f"Memories/{memory_id}/evidence/"
    paths: list[str] = []
    for match in _WIKILINK.finditer(markdown):
        target = match.group(1)
        candidate = target if target.endswith(".md") else f"{target}.md"
        if not candidate.startswith(prefix):
            continue
        try:
            paths.append(_managed_path(memory_id, "evidence", candidate))
        except ValueError:
            continue
    return tuple(dict.fromkeys(paths))


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _load_evidence(
    memory_store: MarkdownMemoryStore,
    memory_id: str,
    paths: Iterable[str],
) -> tuple[tuple[str, str, str], ...]:
    loaded: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for path in paths:
        if len(loaded) >= _MAX_EVIDENCE_NOTES:
            break
        normalized = _managed_path(memory_id, "evidence", path)
        markdown = memory_store.read_text(normalized)
        metadata = _frontmatter(markdown, label="Evidence note")
        evidence_id = str(metadata.get("id") or "").strip()
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        loaded.append((evidence_id, normalized, markdown[:_MAX_EVIDENCE_CHARS]))
    return tuple(loaded)


def _json_object(response: Mapping[str, Any]) -> dict[str, Any]:
    candidate = str(response.get("content") or "").strip()
    fenced = _JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Wiki generation must return valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Wiki generation must return one JSON object")
    return value


def _parse_sections(payload: Mapping[str, Any], allowed_ids: set[str]) -> tuple[str, tuple[WikiSection, ...]]:
    if set(payload) != {"title", "sections"}:
        raise ValueError("Wiki generation fields must be exactly title and sections")
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 200:
        raise ValueError("Wiki title must contain 1-200 characters")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections or len(raw_sections) > 12:
        raise ValueError("Wiki sections must be a non-empty array with at most 12 items")
    sections: list[WikiSection] = []
    headings: set[str] = set()
    claim_count = 0
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict) or set(raw_section) != {"heading", "claims"}:
            raise ValueError("each Wiki section must contain heading and claims")
        heading = str(raw_section.get("heading") or "").strip()
        if not heading or len(heading) > 200 or heading.casefold() in headings:
            raise ValueError("Wiki section headings must be unique and contain 1-200 characters")
        headings.add(heading.casefold())
        raw_claims = raw_section.get("claims")
        if not isinstance(raw_claims, list) or not raw_claims:
            raise ValueError("each Wiki section must contain grounded claims")
        claims: list[WikiClaim] = []
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, dict) or set(raw_claim) != {"text", "evidence_ids"}:
                raise ValueError("each Wiki claim must contain text and evidence_ids")
            text = str(raw_claim.get("text") or "").strip()
            ids = _string_tuple(raw_claim.get("evidence_ids"))
            if not text or len(text) > 4000 or "[[" in text or "]]" in text:
                raise ValueError("Wiki claim text is empty, too long, or contains model-supplied WikiLinks")
            if not ids or any(item not in allowed_ids for item in ids):
                raise ValueError("every Wiki claim must cite only supplied Evidence IDs")
            claims.append(WikiClaim(text=text, evidence_ids=ids))
            claim_count += 1
        sections.append(WikiSection(heading=heading, claims=tuple(claims)))
    if claim_count > 80:
        raise ValueError("Wiki generation returned too many claims")
    return title, tuple(sections)


def _yaml_list(name: str, values: Iterable[str]) -> list[str]:
    items = list(dict.fromkeys(values))
    if not items:
        return [f"{name}: []"]
    return [f"{name}:", *(f"  - {json.dumps(item, ensure_ascii=False)}" for item in items)]


def render_wiki_page(
    *,
    wiki_id: str,
    memory_id: str,
    title: str,
    sections: Iterable[WikiSection],
    integrated_report_ids: Iterable[str],
    created_at: str,
    updated_at: str,
) -> tuple[str, tuple[str, ...]]:
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id for section in sections for claim in section.claims for evidence_id in claim.evidence_ids
        )
    )
    lines = [
        "---",
        f"id: {json.dumps(wiki_id, ensure_ascii=False)}",
        'type: "wiki"',
        "schema_version: 1",
        f"memory_id: {json.dumps(memory_id, ensure_ascii=False)}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        'status: "current"',
        *_yaml_list("integrated_report_ids", integrated_report_ids),
        *_yaml_list("evidence_ids", evidence_ids),
        f"created_at: {json.dumps(created_at, ensure_ascii=False)}",
        f"updated_at: {json.dumps(updated_at, ensure_ascii=False)}",
        'origin: "wiki_generation"',
        "tags:",
        "  - paperpilot",
        "  - wiki",
        "---",
        "",
        f"# {title}",
    ]
    for section in sections:
        lines.extend(("", f"## {section.heading}", ""))
        for claim in section.claims:
            links = " ".join(
                build_wikilink(
                    f"Memories/{memory_id}/evidence/{evidence_id}.md",
                    "Evidence",
                )
                for evidence_id in claim.evidence_ids
            )
            lines.append(f"- {claim.text} {links}")
    return "\n".join(lines).rstrip() + "\n", evidence_ids


def list_wiki_pages(memory_store: MarkdownMemoryStore, memory_id: str) -> tuple[dict[str, str], ...]:
    validate_memory_id(memory_id)
    memory_store.get_memory(memory_id)
    directory = memory_store.root / memory_relative_path(memory_id) / "wiki"
    if not directory.exists():
        return ()
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Wiki directory is not a safe directory")
    pages: list[dict[str, str]] = []
    for path in sorted(directory.glob("*.md"), key=lambda item: item.name.casefold()):
        if path.name.casefold() == "index.md" or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(memory_store.root).as_posix()
        markdown = memory_store.read_text(relative)
        metadata = _frontmatter(markdown, label="Wiki page")
        if metadata.get("type") != "wiki" or metadata.get("memory_id") != memory_id:
            continue
        pages.append(
            {
                "wiki_id": str(metadata.get("id") or path.stem),
                "title": str(metadata.get("title") or path.stem),
                "path": relative,
                "status": str(metadata.get("status") or "current"),
                "updated_at": str(metadata.get("updated_at") or ""),
            }
        )
    return tuple(pages)


async def generate_wiki_draft(
    memory_store: MarkdownMemoryStore,
    policy: Any,
    memory_id: str,
    source_report_path: str,
    *,
    target_path: str | None = None,
) -> WikiDraft:
    validate_memory_id(memory_id)
    memory_store.get_memory(memory_id)
    report_path = _report_path(memory_id, source_report_path)
    report_markdown = memory_store.read_text(report_path)
    report_metadata = _frontmatter(report_markdown, label="Research report")
    report_id = str(report_metadata.get("id") or PurePosixPath(report_path).stem)

    action = "create" if target_path is None else "update"
    existing_markdown = ""
    expected_target_hash: str | None = None
    existing_metadata: dict[str, Any] = {}
    if target_path is None:
        wiki_id = f"Wiki-{uuid.uuid4().hex}"
        resolved_target = f"Memories/{memory_id}/wiki/{wiki_id}.md"
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    else:
        resolved_target = _wiki_path(memory_id, target_path)
        existing_markdown = memory_store.read_text(resolved_target)
        expected_target_hash = _sha256_text(existing_markdown)
        existing_metadata = _frontmatter(existing_markdown, label="Wiki page")
        wiki_id = str(existing_metadata.get("id") or "")
        if not _SAFE_WIKI_ID.fullmatch(wiki_id):
            raise ValueError("existing Wiki page has an invalid id")
        if existing_metadata.get("type") != "wiki" or existing_metadata.get("memory_id") != memory_id:
            raise ValueError("existing Wiki page identity does not match the selected Memory")
        created_at = str(existing_metadata.get("created_at") or "").strip()
        if not created_at:
            raise ValueError("existing Wiki page has no created_at")

    paths = [*_evidence_paths(memory_id, report_markdown)]
    if existing_markdown:
        paths.extend(_evidence_paths(memory_id, existing_markdown))
    evidence = _load_evidence(memory_store, memory_id, paths)
    if not evidence:
        raise ValueError("the selected report contains no readable Evidence links")
    allowed_ids = {item[0] for item in evidence}
    context = {
        "mode": action,
        "report": {"path": report_path, "markdown": report_markdown[:_MAX_REPORT_CHARS]},
        "existing_wiki": (
            {"path": resolved_target, "markdown": existing_markdown[:_MAX_EXISTING_PAGE_CHARS]}
            if existing_markdown
            else None
        ),
        "evidence": [
            {"evidence_id": evidence_id, "path": path, "markdown": markdown} for evidence_id, path, markdown in evidence
        ],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Create a durable Wiki topic page from the supplied research report and Evidence notes. "
                "For update mode, return the complete revised knowledge page, preserving still-supported useful knowledge. "
                "Do not invent facts, paths, citations, or Evidence IDs. Distinguish uncertainty and conflicts in claim text. "
                "Return exactly one JSON object: "
                '{"title":"...","sections":[{"heading":"...","claims":'
                '[{"text":"...","evidence_ids":["Evidence-..."]}]}]}. '
                "Every claim needs at least one ID from the supplied evidence list. Do not emit Markdown or WikiLinks."
            ),
        },
        {"role": "user", "content": "WIKI_CONTEXT_JSON:\n" + json.dumps(context, ensure_ascii=False)},
    ]
    response = await call_policy(policy, messages, [])
    title, sections = _parse_sections(_json_object(response), allowed_ids)
    integrated = tuple(dict.fromkeys((*_string_tuple(existing_metadata.get("integrated_report_ids")), report_id)))
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    markdown, evidence_ids = render_wiki_page(
        wiki_id=wiki_id,
        memory_id=memory_id,
        title=title,
        sections=sections,
        integrated_report_ids=integrated,
        created_at=created_at,
        updated_at=generated_at,
    )
    return WikiDraft(
        memory_id=memory_id,
        action=action,
        wiki_id=wiki_id,
        target_path=resolved_target,
        title=title,
        markdown=markdown,
        source_report_path=report_path,
        source_report_hash=_sha256_text(report_markdown),
        evidence_ids=evidence_ids,
        integrated_report_ids=integrated,
        expected_target_hash=expected_target_hash,
        created_at=created_at,
        generated_at=generated_at,
    )


def validate_wiki_draft(memory_store: MarkdownMemoryStore, draft: WikiDraft) -> None:
    validate_memory_id(draft.memory_id)
    memory_store.get_memory(draft.memory_id)
    if draft.action not in {"create", "update"} or not _SAFE_WIKI_ID.fullmatch(draft.wiki_id):
        raise ValueError("Wiki draft identity is invalid")
    target = _wiki_path(draft.memory_id, draft.target_path)
    if PurePosixPath(target).stem != draft.wiki_id:
        raise ValueError("Wiki target path does not match wiki_id")
    report_path = _report_path(draft.memory_id, draft.source_report_path)
    report = memory_store.read_text(report_path)
    if _sha256_text(report) != draft.source_report_hash:
        raise MemoryWriteConflictError("source report changed after the Wiki preview was generated")

    try:
        current = memory_store.read_text(target)
    except FileNotFoundError:
        current = None
    exact_reuse = current == draft.markdown
    if draft.action == "create" and not exact_reuse:
        if draft.expected_target_hash is not None or current is not None:
            raise MemoryWriteConflictError("Wiki target was created after the preview was generated")
    elif draft.action == "update" and not exact_reuse:
        if not draft.expected_target_hash or current is None:
            raise MemoryWriteConflictError("Wiki update target is unavailable")
        if _sha256_text(current) != draft.expected_target_hash:
            raise MemoryWriteConflictError("Wiki page changed after the preview was generated")

    metadata = _frontmatter(draft.markdown, label="Wiki draft")
    validate_frontmatter(metadata)
    required = {
        "id": draft.wiki_id,
        "type": "wiki",
        "schema_version": 1,
        "memory_id": draft.memory_id,
        "title": draft.title,
        "status": "current",
        "integrated_report_ids": list(draft.integrated_report_ids),
        "evidence_ids": list(draft.evidence_ids),
        "created_at": draft.created_at,
        "updated_at": draft.generated_at,
        "origin": "wiki_generation",
        "tags": ["paperpilot", "wiki"],
    }
    if set(metadata) != set(required):
        raise ValueError("Wiki draft frontmatter fields do not match the Wiki schema")
    for name, expected in required.items():
        if metadata.get(name) != expected:
            raise ValueError(f"Wiki draft frontmatter {name} does not match the validated draft")

    linked_paths = _evidence_paths(draft.memory_id, draft.markdown)
    loaded = _load_evidence(memory_store, draft.memory_id, linked_paths)
    linked_ids = tuple(dict.fromkeys(item[0] for item in loaded))
    if linked_ids != draft.evidence_ids or set(linked_ids) != set(draft.evidence_ids):
        raise ValueError("Wiki draft Evidence links do not match evidence_ids")
    report_id = str(_frontmatter(report, label="Research report").get("id") or PurePosixPath(report_path).stem)
    if report_id not in draft.integrated_report_ids:
        raise ValueError("Wiki draft does not record its source report")
    if not re.search(rf"^#\s+{re.escape(draft.title)}\s*$", draft.markdown, re.MULTILINE):
        raise ValueError("Wiki draft heading does not match its title")


def render_wiki_index(memory_store: MarkdownMemoryStore, draft: WikiDraft) -> str:
    pages = {item["path"]: item for item in list_wiki_pages(memory_store, draft.memory_id)}
    pages[draft.target_path] = {
        "path": draft.target_path,
        "title": draft.title,
        "wiki_id": draft.wiki_id,
        "status": "current",
        "updated_at": draft.generated_at,
    }
    lines = [
        "---",
        f'id: "WikiIndex-{draft.memory_id[2:]}"',
        'type: "wiki_index"',
        "schema_version: 1",
        f"memory_id: {json.dumps(draft.memory_id, ensure_ascii=False)}",
        'title: "Wiki"',
        'status: "current"',
        f"created_at: {json.dumps(draft.generated_at, ensure_ascii=False)}",
        f"updated_at: {json.dumps(draft.generated_at, ensure_ascii=False)}",
        'origin: "wiki_generation"',
        "tags:",
        "  - paperpilot",
        "  - wiki",
        "---",
        "",
        "# Wiki",
        "",
    ]
    for page in sorted(pages.values(), key=lambda item: (item["title"].casefold(), item["path"])):
        lines.append(f"- {build_wikilink(page['path'], page['title'])}")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "generate_wiki_draft",
    "list_wiki_pages",
    "render_wiki_index",
    "render_wiki_page",
    "validate_wiki_draft",
]
