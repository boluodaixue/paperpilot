"""Production bootstrap for the PaperPilot research workflow.

This module is deliberately small: it wires the homogeneous Research AgentGraph
to the configured model, tools, checkpointer, and Markdown Memory Store.  CLI,
Web, and evaluation callers share this entry instead of rebuilding product
architecture around the graph.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable

import yaml
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from ..models.model_router import ModelRouter
from ..tools import (
    ArxivReaderTool,
    BrowserTool,
    CalculatorTool,
    CodeSandboxTool,
    FileReaderTool,
    MockBrowserTool,
    MockWebSearchTool,
    NotepadTool,
    WebSearchTool,
)
from ..utils.tracing import flush_tracing, shutdown_tracing
from .memory import MarkdownMemoryStore
from .models import (
    AgentLimits,
    ExecutionIdentity,
    ResearchBrief,
    ResearchWorkflowResult,
)
from .workflow import (
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

__all__ = [
    "PROJECT_ROOT",
    "ResearchRuntime",
    "build_research_runtime",
    "build_research_tools",
    "limits_from_config",
    "load_config",
    "setup_logging",
    "vault_root_from_config",
]


def load_config(config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load the active PaperPilot YAML configuration."""
    path = Path(config_path) if config_path else PROJECT_ROOT / "configs" / "default.yaml"
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError("PaperPilot configuration must be a YAML object")
    return value


def setup_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, str(log_level).upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _sampling_kwargs(config: dict[str, Any], module: str, backend: str) -> dict[str, Any]:
    sampling = config.get("model", {}).get("backend_sampling", {})
    result = dict(sampling.get(backend, {}))
    result.update(sampling.get("modules", {}).get(module, {}))
    return result


def _build_policy(config: dict[str, Any]) -> Any:
    model = config.get("model", {})
    backend = model.get("backend_mapping", {}).get(
        "research", model.get("backend", "vllm")
    )
    return ModelRouter.create_backend(
        backend,
        **_sampling_kwargs(config, "research", backend),
    )


def build_research_tools(config: dict[str, Any]) -> list[Any]:
    """Build the tools exposed to every homogeneous Research Agent."""
    tools_config = config.get("tools", {})
    mock_mode = bool(tools_config.get("web_search", {}).get("mock_mode", False))
    enabled = tools_config.get("enabled")

    available: dict[str, Any] = {
        "web_search": MockWebSearchTool() if mock_mode else WebSearchTool(),
        "browser": MockBrowserTool() if mock_mode else BrowserTool(),
        "arxiv_reader": ArxivReaderTool(use_mock=mock_mode),
        "file_reader": FileReaderTool(allowed_base_dir=None),
        "code_sandbox": CodeSandboxTool(use_mock=mock_mode),
        "calculator": CalculatorTool(),
        "notepad": NotepadTool(),
    }
    if enabled is None:
        return list(available.values())
    if not isinstance(enabled, list):
        raise ValueError("tools.enabled must be a list of tool names")
    unknown = [name for name in enabled if name not in available]
    if unknown:
        raise ValueError(f"unknown research tools: {', '.join(unknown)}")
    return [available[name] for name in enabled]


def _research_config(config: dict[str, Any]) -> Mapping[str, Any]:
    research = config.get("research", {})
    if not isinstance(research, Mapping):
        raise ValueError("research configuration must be a mapping")
    return research


def limits_from_config(config: dict[str, Any]) -> AgentLimits:
    values = _research_config(config).get("limits", {})
    allowed = AgentLimits.__dataclass_fields__.keys()
    limits = AgentLimits(**{key: values[key] for key in allowed if key in values})
    limits.validate()
    return limits


def vault_root_from_config(config: dict[str, Any]) -> Path:
    """Resolve the configured Vault root, with legacy ``memory_root`` fallback."""
    research = _research_config(config)
    if "vault_root" in research:
        key = "vault_root"
        configured = research[key]
    elif "memory_root" in research:
        key = "memory_root"
        configured = research[key]
    else:
        key = "vault_root"
        configured = "memory"

    try:
        raw_path = os.fspath(configured)
    except TypeError as exc:
        raise ValueError(
            f"research.{key} must be a non-empty path-like string"
        ) from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"research.{key} must be a non-empty path-like string")

    path = Path(raw_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _report_review_enabled(config: dict[str, Any]) -> bool:
    research = _research_config(config)
    report_review = research.get("report_review", {})
    if not isinstance(report_review, Mapping):
        raise ValueError("research.report_review must be a mapping")
    value = report_review.get("enabled", False)
    if not isinstance(value, bool):
        raise ValueError("research.report_review.enabled must be a boolean")
    return value


class ResearchRuntime:
    """Shared production facade for starting and resuming root research runs."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        policy: Any,
        tools: Iterable[Any],
        memory_store: MarkdownMemoryStore,
        checkpointer: BaseCheckpointSaver,
        limits: AgentLimits,
        report_review_enabled: bool = False,
    ) -> None:
        self.config = config
        self.policy = policy
        self.tools = list(tools)
        self.memory_store = memory_store
        self.checkpointer = checkpointer
        self.limits = limits
        self.report_review_enabled = report_review_enabled
        self.graph = build_research_workflow(
            policy,
            self.tools,
            memory_store,
            checkpointer=checkpointer,
            report_review_enabled=report_review_enabled,
        )

    @staticmethod
    def new_thread_id() -> str:
        return f"research-{uuid.uuid4().hex}"

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        root_thread_id = thread_id or self.new_thread_id()
        identity = ExecutionIdentity(
            thread_id=root_thread_id,
            parent_thread_id=None,
            root_thread_id=root_thread_id,
            depth=0,
        )
        return await self.graph.ainvoke(
            create_research_workflow_state(question, identity, self.limits),
            config={"configurable": {"thread_id": root_thread_id}},
        )

    async def review(
        self,
        thread_id: str,
        action: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        return await resume_research_workflow(
            self.graph,
            thread_id=thread_id,
            action=action,
            feedback=feedback,
        )

    async def run_auto_confirmed(
        self,
        question: str,
        *,
        thread_id: str | None = None,
    ) -> ResearchWorkflowResult:
        root_thread_id = thread_id or self.new_thread_id()
        await self.start(question, thread_id=root_thread_id)
        final = await self.review(root_thread_id, "confirm")
        result = final.get("workflow_result")
        if not isinstance(result, ResearchWorkflowResult):
            raise RuntimeError("research workflow ended without a structured result")
        return result

    async def get_state(self, thread_id: str) -> dict[str, Any]:
        snapshot = await self.graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        return dict(snapshot.values)

    def read_memory(self, relative_path: str) -> str:
        return self.memory_store.read_text(relative_path)

    async def close(self, *, shutdown: bool = False) -> None:
        await WebSearchTool.close_session()
        flush_tracing()
        if shutdown:
            shutdown_tracing()


def build_research_runtime(
    config: dict[str, Any] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    policy: Any | None = None,
    tools: Iterable[Any] | None = None,
    memory_store: MarkdownMemoryStore | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> ResearchRuntime:
    """Construct the single production Research Workflow dependency graph."""
    effective_config = config if config is not None else load_config(config_path)
    return ResearchRuntime(
        config=effective_config,
        policy=policy if policy is not None else _build_policy(effective_config),
        tools=list(tools) if tools is not None else build_research_tools(effective_config),
        memory_store=(
            memory_store
            if memory_store is not None
            else MarkdownMemoryStore(vault_root_from_config(effective_config))
        ),
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        limits=limits_from_config(effective_config),
        report_review_enabled=_report_review_enabled(effective_config),
    )
