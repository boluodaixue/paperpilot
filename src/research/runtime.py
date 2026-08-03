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
from contextlib import contextmanager
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
from ..utils.tracing import (
    flush_tracing,
    shutdown_tracing,
    trace_block,
    trace_context,
)
from .memory import MarkdownMemoryStore
from .memory_dialogue import (
    answer_memory as answer_from_memory,
    propose_memory_note as propose_note_from_memory,
)
from .memory_import import (
    prepare_memory_file_import as prepare_file_import,
    prepare_memory_text_import as prepare_text_import,
    prepare_memory_url_import as prepare_url_import,
)
from .models import (
    AgentLimits,
    ExecutionIdentity,
    MemoryAnswer,
    MemoryDescriptor,
    MemoryImportDuplicate,
    MemoryImportProposal,
    MemoryNoteProposal,
    ResearchBrief,
    ResearchWorkflowResult,
)
from .workflow import (
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from .vault import LEGACY_MEMORY_ID, scan_legacy_memory_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _memory_trace(name: str, memory_id: str | None, *, run_type: str = "chain"):
    """Attach bounded Memory identity to one existing runtime operation."""
    metadata = {"memory_id": memory_id} if memory_id is not None else None
    with trace_context(metadata=metadata, tags=["memory"]):
        with trace_block(name, run_type=run_type, tags=["memory"]) as observation:
            if memory_id is not None:
                observation.add_metadata({"memory_id": memory_id})
            yield observation


def _memory_import_preview_trace(
    value: Any,
    *,
    memory_id: str | None = None,
) -> dict[str, Any]:
    """Return path-only import trace output without constraining delegated results."""
    if isinstance(value, MemoryImportDuplicate):
        status = "duplicate"
    elif isinstance(value, MemoryImportProposal):
        status = "proposal"
    else:
        status = "result"
    return {
        "status": status,
        "memory_id": getattr(value, "memory_id", memory_id),
        "proposal_id": getattr(value, "proposal_id", None),
        "attachment_path": getattr(value, "attachment_path", None),
        "import_path": getattr(value, "import_path", None),
        "note_path": getattr(value, "note_path", None),
    }

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
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        if memory_id == LEGACY_MEMORY_ID:
            raise ValueError(
                "M-legacy is read-only; migrate it or select a managed Memory "
                "before starting research"
            )
        root_thread_id = thread_id or self.new_thread_id()
        identity = ExecutionIdentity(
            thread_id=root_thread_id,
            parent_thread_id=None,
            root_thread_id=root_thread_id,
            depth=0,
        )
        with _memory_trace("paperpilot.research.start", memory_id) as observation:
            result = await self.graph.ainvoke(
                create_research_workflow_state(
                    question,
                    identity,
                    self.limits,
                    memory_id=memory_id,
                ),
                config={"configurable": {"thread_id": root_thread_id}},
            )
            brief = result.get("brief")
            observation.add_output(
                {
                    "memory_id": memory_id,
                    "thread_id": root_thread_id,
                    "memory_paths": list(getattr(brief, "memory_paths", ())),
                }
            )
            return result

    async def review(
        self,
        thread_id: str,
        action: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        state = await self.get_state(thread_id)
        memory_id = state.get("memory_id")
        with _memory_trace("paperpilot.research.review", memory_id) as observation:
            result = await resume_research_workflow(
                self.graph,
                thread_id=thread_id,
                action=action,
                feedback=feedback,
            )
            workflow_result = result.get("workflow_result")
            manifest = getattr(workflow_result, "memory_manifest", None)
            observation.add_output(
                {
                    "memory_id": memory_id,
                    "thread_id": thread_id,
                    "action": action,
                    "write_paths": (
                        [
                            manifest.report_path,
                            *manifest.evidence_paths,
                            *manifest.source_paths,
                        ]
                        if manifest is not None
                        else []
                    ),
                }
            )
            return result

    async def run_auto_confirmed(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        memory_id: str | None = None,
    ) -> ResearchWorkflowResult:
        root_thread_id = thread_id or self.new_thread_id()
        await self.start(
            question,
            thread_id=root_thread_id,
            memory_id=memory_id,
        )
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

    def create_memory(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> MemoryDescriptor:
        return self.memory_store.create_memory(title, memory_id=memory_id)

    def list_memories(self) -> tuple[MemoryDescriptor, ...]:
        return self.memory_store.list_memories()

    def get_memory(self, memory_id: str) -> MemoryDescriptor:
        return self.memory_store.get_memory(memory_id)

    def get_memory_option(self, memory_id: str) -> dict[str, Any]:
        """Resolve one CLI/Web selection without weakening MemoryDescriptor."""
        if memory_id == LEGACY_MEMORY_ID:
            files = scan_legacy_memory_markdown(self.memory_store.root)
            if not files:
                raise FileNotFoundError("legacy Memory contains no Markdown files")
            return {
                "memory_id": LEGACY_MEMORY_ID,
                "title": "Existing Memory (read-only)",
                "relative_path": None,
                "created_at": None,
                "updated_at": None,
                "read_only": True,
                "can_migrate": True,
                "file_count": len(files),
            }
        descriptor = self.get_memory(memory_id)
        return {
            "memory_id": descriptor.memory_id,
            "title": descriptor.title,
            "relative_path": descriptor.relative_path,
            "created_at": descriptor.created_at,
            "updated_at": descriptor.updated_at,
            "read_only": False,
            "can_migrate": False,
            "file_count": None,
        }

    def list_memory_options(self) -> tuple[dict[str, Any], ...]:
        """List managed selections plus the virtual read-only legacy option."""
        options = [self.get_memory_option(item.memory_id) for item in self.list_memories()]
        if scan_legacy_memory_markdown(self.memory_store.root):
            options.append(self.get_memory_option(LEGACY_MEMORY_ID))
        return tuple(options)

    def prepare_legacy_memory_migration(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> dict[str, object]:
        with _memory_trace(
            "paperpilot.memory.legacy_migration.prepare", LEGACY_MEMORY_ID
        ) as observation:
            proposal = self.memory_store.prepare_legacy_memory_migration(
                title,
                memory_id,
            )
            files = proposal["files"]
            observation.add_output(
                {
                    "status": "proposal",
                    "source_memory_id": LEGACY_MEMORY_ID,
                    "target_memory_id": proposal["target_memory_id"],
                    "proposal_id": proposal["proposal_id"],
                    "source_files": [item["source_path"] for item in files],
                    "target_files": [item["target_path"] for item in files],
                }
            )
            return proposal

    def commit_legacy_memory_migration(
        self,
        proposal: Mapping[str, object],
    ) -> MemoryDescriptor:
        target_memory_id = str(proposal.get("target_memory_id") or "")
        with _memory_trace(
            "paperpilot.memory.legacy_migration.commit", target_memory_id or None
        ) as observation:
            descriptor = self.memory_store.commit_legacy_memory_migration(proposal)
            observation.add_output(
                {
                    "status": "committed",
                    "source_memory_id": LEGACY_MEMORY_ID,
                    "memory_id": descriptor.memory_id,
                    "home_path": f"{descriptor.relative_path}Home.md",
                }
            )
            return descriptor

    async def answer_memory(
        self,
        memory_id: str,
        question: str,
    ) -> MemoryAnswer:
        with _memory_trace("paperpilot.memory.answer", memory_id) as observation:
            answer = await answer_from_memory(
                self.memory_store,
                self.policy,
                memory_id,
                question,
            )
            observation.add_output(
                {
                    "memory_id": memory_id,
                    "answer_id": answer.answer_id,
                    "retrieved_files": [
                        citation.relative_path for citation in answer.citations
                    ],
                    "insufficient_evidence_count": len(answer.insufficient_evidence),
                }
            )
            return answer

    async def propose_memory_note(
        self,
        answer: MemoryAnswer,
    ) -> MemoryNoteProposal:
        with _memory_trace(
            "paperpilot.memory.note.prepare", answer.memory_id
        ) as observation:
            proposal = await propose_note_from_memory(
                self.memory_store,
                self.policy,
                answer,
            )
            observation.add_output(
                {
                    "memory_id": proposal.memory_id,
                    "proposal_id": proposal.proposal_id,
                    "target_path": proposal.target_path,
                    "home_path": proposal.home_path,
                    "source_paths": list(proposal.source_paths),
                }
            )
            return proposal

    def commit_memory_note(self, proposal: MemoryNoteProposal) -> dict[str, str]:
        with _memory_trace(
            "paperpilot.memory.note.commit", proposal.memory_id
        ) as observation:
            result = self.memory_store.commit_memory_note(proposal)
            observation.add_output(dict(result))
            return result

    async def prepare_memory_file_import(
        self,
        memory_id: str,
        file_name: str,
        content: bytes,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        with _memory_trace(
            "paperpilot.memory.import.prepare", memory_id
        ) as observation:
            result = await prepare_file_import(
                self.memory_store,
                self.policy,
                memory_id,
                file_name,
                content,
            )
            observation.add_output(
                _memory_import_preview_trace(result, memory_id=memory_id)
            )
            return result

    async def prepare_memory_text_import(
        self,
        memory_id: str,
        title: str,
        text: str,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        with _memory_trace(
            "paperpilot.memory.import.prepare", memory_id
        ) as observation:
            result = await prepare_text_import(
                self.memory_store,
                self.policy,
                memory_id,
                title,
                text,
            )
            observation.add_output(
                _memory_import_preview_trace(result, memory_id=memory_id)
            )
            return result

    async def prepare_memory_url_import(
        self,
        memory_id: str,
        url: str,
    ) -> MemoryImportProposal | MemoryImportDuplicate:
        with _memory_trace(
            "paperpilot.memory.import.prepare", memory_id
        ) as observation:
            result = await prepare_url_import(
                self.memory_store,
                self.policy,
                memory_id,
                url,
            )
            observation.add_output(
                _memory_import_preview_trace(result, memory_id=memory_id)
            )
            return result

    def commit_memory_import(self, proposal: MemoryImportProposal) -> dict[str, Any]:
        with _memory_trace(
            "paperpilot.memory.import.commit", getattr(proposal, "memory_id", None)
        ) as observation:
            result = self.memory_store.commit_memory_import(proposal)
            observation.add_output(
                {
                    key: value
                    for key, value in result.items()
                    if key
                    in {
                        "status",
                        "memory_id",
                        "attachment_path",
                        "import_path",
                        "note_path",
                        "home_path",
                        "wikilinks",
                    }
                }
                if isinstance(result, Mapping)
                else {"status": "result"}
            )
            return result

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
