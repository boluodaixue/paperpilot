"""Report-boundary hygiene for model-produced claims and disclosures."""

from __future__ import annotations

import re


_EVIDENCE_MARKER = re.compile(r"\[\[EVIDENCE:[^\]]+\]\]", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RAW_URL = re.compile(r"https?://[^\s<>\[\](){}\"']+", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_SPACE = re.compile(r"\s+")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？；;])\s+|[\r\n]+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_FACT_SIGNAL = re.compile(
    r"\b(?:reports?|reported|found|showed|requires?|required|approved|"
    r"increased|decreased|compared|measured|identified)\b|"
    r"(?:报告|显示|发现|要求|批准|增加|降低|相比|测得|识别)",
    re.IGNORECASE,
)
_NAVIGATION_SIGNAL = re.compile(
    r"\b(?:cookie|privacy policy|terms of use|skip to content|sign in|"
    r"subscribe|navigation|all rights reserved|orcid|download pdf|share this)\b",
    re.IGNORECASE,
)


def normalize_report_text(value: object, *, limit: int = 800) -> str:
    """Return readable prose with transport and navigation syntax removed."""

    text = str(value or "").strip()
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _MARKDOWN_LINK.sub(lambda match: match.group(1), text)
    text = _EVIDENCE_MARKER.sub("", text)
    text = _WIKILINK.sub("", text)
    text = _RAW_URL.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _SPACE.sub(" ", text).strip(" \t\r\n-|—")
    if len(text) > limit:
        text = text[: max(1, limit - 3)].rstrip() + "..."
    return text


def reportable_claim_text(value: object, *, limit: int = 800) -> str | None:
    """Normalize an atomic claim or quarantine raw-page-shaped content."""

    original = str(value or "").strip()
    if not original or len(original) > limit:
        return None
    if _MARKDOWN_IMAGE.search(original) or _HTML_TAG.search(original):
        return None
    if _TABLE_ROW.search(original) or original.count("\n") > 2:
        return None
    if len(_RAW_URL.findall(original)) > 1:
        return None
    if len(_NAVIGATION_SIGNAL.findall(original)) >= 2:
        return None
    clean = normalize_report_text(original, limit=limit)
    if len(clean) < 8:
        return None
    return clean


def derive_atomic_claim(
    value: object,
    *,
    question_context: str = "",
    limit: int = 600,
) -> str | None:
    """Recover one concise assertion from a noisy Evidence finding.

    This is deliberately extractive: it selects existing prose and removes
    transport syntax, rather than asking a model to invent a summary.
    """

    direct = reportable_claim_text(value, limit=limit)
    if direct is not None:
        return direct
    raw = str(value or "").strip()
    if not raw:
        return None
    context_terms = {
        item.casefold()
        for item in re.findall(r"[A-Za-z0-9]{3,}", question_context)
    }
    candidates: list[tuple[float, int, str]] = []
    for position, fragment in enumerate(_SENTENCE_BOUNDARY.split(raw)):
        if _TABLE_SEPARATOR.search(fragment):
            continue
        clean = normalize_report_text(fragment, limit=limit)
        if len(clean) < 20 or len(_NAVIGATION_SIGNAL.findall(clean)) >= 2:
            continue
        terms = {
            item.casefold() for item in re.findall(r"[A-Za-z0-9]{3,}", clean)
        }
        overlap = len(terms & context_terms)
        score = overlap * 4.0
        score += 2.0 if re.search(r"\d", clean) else 0.0
        score += 2.0 if _FACT_SIGNAL.search(clean) else 0.0
        score += min(len(clean), 240) / 240.0
        score -= 2.0 if clean.endswith((':', '：')) else 0.0
        candidates.append((score, -position, clean))
    if not candidates:
        return None
    selected = max(candidates)[2]
    return reportable_claim_text(selected, limit=limit)


def safe_disclosure_text(value: object, *, limit: int = 240) -> str:
    """Bound user-visible limitation text without leaking raw page material."""

    return normalize_report_text(value, limit=limit).replace("|", "/")
