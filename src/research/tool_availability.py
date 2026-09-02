"""Deterministic classification for external research-tool availability failures."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .models import ToolAvailabilityAlert

__all__ = [
    "availability_alert_from_event",
    "classify_fallback_backend_alerts",
    "classify_tool_availability",
]


def _sanitize_error(error: str) -> str:
    """Keep diagnostics useful without publishing credentials in alerts."""
    normalized = " ".join(error.split())
    normalized = re.sub(
        r"(?i)\b(authorization|api[_ -]?key|access[_ -]?token)\b\s*[:=]?\s*" r"(?:bearer\s+)?[^\s,;]+",
        r"\1=[redacted]",
        normalized,
    )
    normalized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", normalized)
    return normalized[:500]


def _target_from_error(arguments: dict[str, object], error: str) -> str:
    candidates = [str(arguments.get(key) or "").strip() for key in ("url", "source_ref", "path", "query")]
    candidates.extend(re.findall(r"https?://[^\s'\"]+", error))
    for candidate in candidates:
        parsed = urlparse(candidate.rstrip("),.;"))
        if parsed.hostname:
            return parsed.hostname.lower()
    return ""


def _alert(
    *,
    tool: str,
    category: str,
    scope: str,
    target: str,
    message: str,
    action_required: str,
    circuit_open: bool,
    error: str,
) -> ToolAvailabilityAlert:
    normalized_error = _sanitize_error(error)
    encoded = "|".join((tool, category, scope, target, normalized_error))
    return ToolAvailabilityAlert(
        alert_id="tool-alert-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
        tool=tool,
        category=category,
        scope=scope,
        target=target,
        message=message,
        action_required=action_required,
        circuit_open=circuit_open,
        error=normalized_error,
    )


def classify_tool_availability(
    tool_name: str,
    error: str | None,
    arguments: dict[str, object] | None = None,
) -> ToolAvailabilityAlert | None:
    """Return a user-visible availability alert for actionable external failures."""
    if not error:
        return None
    arguments = arguments or {}
    lowered = " ".join(error.lower().split())
    target = _target_from_error(arguments, error)

    if "all web search backends unavailable" in lowered:
        return _alert(
            tool=tool_name,
            category="service_unavailable",
            scope="tool",
            target=tool_name,
            message="所有已配置的网页搜索服务均不可用，已暂停网页检索。",
            action_required="请检查 Tavily/秘塔/Exa 的密钥或网络状态后再继续。",
            circuit_open=True,
            error=error,
        )

    if "all academic metadata backends unavailable" in lowered:
        return _alert(
            tool=tool_name,
            category="service_unavailable",
            scope="tool",
            target=target or tool_name,
            message=f"{tool_name} 的所有学术信息后端均不可用，已暂停该工具。",
            action_required="请检查网络/证书，或配置可用的 OpenAlex、Semantic Scholar 后端。",
            circuit_open=True,
            error=error,
        )

    quota_markers = (
        "not enough money",
        "package quota",
        "insufficient_quota",
        "quota exceeded",
        "quota exhausted",
        "billing hard limit",
    )
    if any(marker in lowered for marker in quota_markers):
        return _alert(
            tool=tool_name,
            category="service_unavailable",
            scope="tool",
            target=target or tool_name,
            message=f"外部信息服务 {tool_name} 的额度或套餐不可用，已暂停该工具。",
            action_required="请补充服务额度、检查套餐，或切换可用的信息源后再继续。",
            circuit_open=True,
            error=error,
        )

    auth_markers = (
        "invalid api key",
        "authentication failed",
        "unauthorized",
        "missing api key",
        "api_key is not configured",
        "需要 api key",
        "permission denied",
    )
    if any(marker in lowered for marker in auth_markers):
        return _alert(
            tool=tool_name,
            category="service_unavailable",
            scope="tool",
            target=target or tool_name,
            message=f"外部信息服务 {tool_name} 的认证或权限不可用，已暂停该工具。",
            action_required="请检查 API 凭据和服务权限后再继续。",
            circuit_open=True,
            error=error,
        )

    if "openalex" in lowered and "404" in lowered and ("unexpected mimetype" in lowered or "/works/" in lowered):
        return _alert(
            tool=tool_name,
            category="adapter_error",
            scope="tool",
            target="api.openalex.org",
            message=f"{tool_name} 的 OpenAlex 标识符适配失败，已暂停该工具以避免重复无效调用。",
            action_required="请修复 arXiv/DOI/OpenAlex ID 转换后再继续使用该工具。",
            circuit_open=True,
            error=error,
        )

    if any(marker in lowered for marker in ("403", "forbidden")):
        return _alert(
            tool=tool_name,
            category="source_blocked",
            scope="source",
            target=target or "unknown source",
            message=f"信息来源 {target or 'unknown source'} 拒绝访问；该来源当前不可用。",
            action_required="请改用该机构的公开镜像、API、论文或其他可访问来源。",
            circuit_open=False,
            error=error,
        )

    certificate_markers = (
        "certificateerror",
        "certificate verify failed",
        "ssl certificate",
        "tls certificate",
    )
    if any(marker in lowered for marker in certificate_markers):
        return _alert(
            tool=tool_name,
            category="source_blocked",
            scope="source",
            target=target or "unknown source",
            message=f"信息来源 {target or 'unknown source'} 的 TLS/证书连接失败。",
            action_required="请检查证书链、代理或网络环境，或改用可信镜像。",
            circuit_open=False,
            error=error,
        )

    if any(marker in lowered for marker in ("429", "rate limit", "too many requests")):
        return _alert(
            tool=tool_name,
            category="service_degraded",
            scope="tool",
            target=target or tool_name,
            message=f"外部信息服务 {tool_name} 正在限流，本次调用不可用。",
            action_required="请稍后重试或切换备用信息源。",
            circuit_open=False,
            error=error,
        )

    return None


def classify_fallback_backend_alerts(
    tool_name: str,
    result: object,
    arguments: dict[str, object] | None = None,
) -> tuple[ToolAvailabilityAlert, ...]:
    """Report failed search backends even when a later fallback succeeds."""
    if tool_name not in {"web_search", "acquire_evidence"} or not isinstance(result, dict):
        return ()
    if not result.get("results") or not result.get("fallback_used"):
        return ()

    alerts: list[ToolAvailabilityAlert] = []
    for failure in result.get("backend_errors") or ():
        if not isinstance(failure, dict):
            continue
        backend = str(failure.get("backend") or "unknown")
        backend_error = str(failure.get("error") or "")
        if not backend_error or backend_error == "no results":
            continue
        backend_tool = f"web_search:{backend}"
        alert = classify_tool_availability(
            backend_tool,
            backend_error,
            arguments,
        )
        if alert is None:
            alert = _alert(
                tool=backend_tool,
                category="service_degraded",
                scope="backend",
                target=backend,
                message=f"网页搜索源 {backend} 本次不可用，已自动切换备用源。",
                action_required="无需中断当前研究；如持续发生，请检查该服务状态或密钥。",
                circuit_open=False,
                error=backend_error,
            )
        alerts.append(alert)
    return tuple(alerts)


def availability_alert_from_event(event: dict[str, object]) -> ToolAvailabilityAlert:
    """Rebuild the public result contract from a checkpointed alert event."""
    return ToolAvailabilityAlert(
        alert_id=str(event.get("alert_id") or ""),
        tool=str(event.get("tool") or ""),
        category=str(event.get("category") or ""),
        scope=str(event.get("scope") or ""),
        target=str(event.get("target") or ""),
        message=str(event.get("message") or ""),
        action_required=str(event.get("action_required") or ""),
        circuit_open=bool(event.get("circuit_open", False)),
        error=str(event.get("error") or ""),
    )
