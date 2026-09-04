from __future__ import annotations

from pathlib import Path

from src.research.models import (
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ToolAvailabilityAlert,
)
from src.research.rendering import render_report
from src.research.tool_availability import (
    classify_fallback_backend_alerts,
    classify_tool_availability,
)


def test_quota_failure_is_a_tool_level_circuit_breaker() -> None:
    alert = classify_tool_availability(
        "web_search",
        "BochaAI error: You do not have enough money or package quota",
        {"query": "current evidence"},
    )

    assert alert is not None
    assert alert.category == "service_unavailable"
    assert alert.scope == "tool"
    assert alert.circuit_open is True
    assert "额度" in alert.message


def test_openalex_identifier_failure_is_an_adapter_circuit_breaker() -> None:
    alert = classify_tool_availability(
        "arxiv_reader",
        (
            "OpenAlex request failed: 404, Attempt to decode JSON with unexpected "
            "mimetype at https://api.openalex.org/works/2412.15115"
        ),
        {"paper_id": "2412.15115"},
    )

    assert alert is not None
    assert alert.category == "adapter_error"
    assert alert.target == "api.openalex.org"
    assert alert.circuit_open is True


def test_source_403_alert_does_not_disable_the_whole_browser() -> None:
    alert = classify_tool_availability(
        "browser",
        "[Browser Error] 403 Forbidden: https://openai.com/index/gpt-4o-system-card/",
        {"url": "https://openai.com/index/gpt-4o-system-card/"},
    )

    assert alert is not None
    assert alert.category == "source_blocked"
    assert alert.scope == "source"
    assert alert.target == "openai.com"
    assert alert.circuit_open is False


def test_unrelated_content_error_is_not_mislabeled_as_service_unavailable() -> None:
    assert (
        classify_tool_availability(
            "file_reader",
            "unsupported document format",
            {"path": "notes.bin"},
        )
        is None
    )


def test_all_academic_backends_unavailable_opens_reader_circuit() -> None:
    alert = classify_tool_availability(
        "arxiv_reader",
        "All academic metadata backends unavailable: arxiv: TLS failure | openalex: 503",
    )

    assert alert is not None
    assert alert.category == "service_unavailable"
    assert alert.circuit_open is True


def test_all_web_search_backends_unavailable_opens_search_circuit() -> None:
    alert = classify_tool_availability(
        "web_search",
        "All web search backends unavailable: tavily: timeout | metaso: 503",
    )

    assert alert is not None
    assert alert.category == "service_unavailable"
    assert alert.scope == "tool"
    assert alert.circuit_open is True


def test_successful_fallback_still_reports_failed_backend() -> None:
    alerts = classify_fallback_backend_alerts(
        "web_search",
        {
            "results": [{"url": "https://example.cn"}],
            "fallback_used": True,
            "backend_errors": [{"backend": "tavily", "error": "Tavily network error: timeout"}],
        },
        {"query": "测试问题"},
    )

    assert len(alerts) == 1
    assert alerts[0].tool == "web_search:tavily"
    assert alerts[0].scope == "backend"
    assert alerts[0].circuit_open is False
    assert "自动切换" in alerts[0].message


def test_alert_diagnostic_redacts_credentials() -> None:
    alert = classify_tool_availability(
        "web_search",
        "Unauthorized api_key=secret-value sk-1234567890abcdef",
    )

    assert alert is not None
    assert "secret-value" not in alert.error
    assert "sk-1234567890abcdef" not in alert.error
    assert "[redacted]" in alert.error


def test_report_preserves_external_information_alert() -> None:
    alert = ToolAvailabilityAlert(
        alert_id="tool-alert-test",
        tool="web_search",
        category="service_unavailable",
        scope="tool",
        target="web_search",
        message="外部信息服务额度不可用。",
        action_required="请补充额度后再继续。",
        circuit_open=True,
    )
    report = render_report(
        ResearchBrief(
            question="测试问题",
            objective="测试外部信息源告警",
            scope=(),
            directions=(),
            constraints=(),
            expected_output="测试报告",
        ),
        ResearchResult(
            task_id="task-alert",
            status=ResearchStatus.FAILED,
            summary="无法继续外部检索。",
            tool_alerts=(alert,),
        ),
        report_note="Report-alert",
        evidence_notes={},
        root_thread_id="root-alert",
    )

    assert "## External Information Availability" in report
    assert alert.message in report
    assert alert.action_required in report


def test_web_progress_has_immediate_unavailable_and_skip_messages() -> None:
    html = Path("web/static/index.html").read_text(encoding="utf-8")

    assert "case 'tool_unavailable'" in html
    assert "外部信息源不可用" in html
    assert "case 'tool_call_skipped_unavailable'" in html
