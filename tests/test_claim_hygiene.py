"""Claim hygiene extracts concise assertions without preserving page transport."""

from src.research.claim_hygiene import derive_atomic_claim, reportable_claim_text


def test_atomic_claim_passes_unchanged() -> None:
    claim = "The trial reported transfusion independence in 32 of 35 participants."

    assert derive_atomic_claim(claim) == claim


def test_noisy_finding_yields_relevant_existing_sentence_without_urls() -> None:
    finding = (
        "Navigation Cookie Privacy Policy Subscribe Download PDF\n"
        "The pivotal trial reported transfusion independence in 32 of 35 participants.\n"
        "See https://example.com/article and https://example.com/orcid for details."
    )

    claim = derive_atomic_claim(
        finding,
        question_context="What clinical efficacy did the pivotal trial report?",
    )

    assert claim == "The pivotal trial reported transfusion independence in 32 of 35 participants."
    assert "http" not in claim


def test_navigation_only_content_remains_quarantined() -> None:
    finding = "| Navigation | Cookie |\n|---|---|\n| Privacy policy | Subscribe |"

    assert reportable_claim_text(finding) is None
    assert derive_atomic_claim(finding) is None
