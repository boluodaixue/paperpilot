"""Production bootstrap for the PaperPilot research workflow.

This module is deliberately small: it wires the homogeneous Research AgentGraph
to the configured model, tools, checkpointer, and Markdown Memory Store.  CLI,
Web, and evaluation callers share this entry instead of rebuilding product
architecture around the graph.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import yaml
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ..models.model_router import ModelRouter
from ..tools import (
    ArxivReaderTool,
    BrowserTool,
    CalculatorTool,
    CodeSandboxTool,
    FileReaderTool,
    EvidenceAcquisitionTool,
    MockBrowserTool,
    MockWebSearchTool,
    NotepadTool,
    WebSearchTool,
    file_reader_scope,
)
from ..utils.tracing import (
    flush_tracing,
    shutdown_tracing,
    trace_block,
    trace_context,
)
from .checkpoint_serde import (
    paperpilot_checkpoint_serializer,
    paperpilot_in_memory_saver,
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
from .memory_workflows import (
    build_legacy_migration_workflow,
    build_memory_import_workflow,
    build_memory_note_workflow,
    continue_memory_workflow,
    create_legacy_migration_workflow_state,
    create_memory_file_import_workflow_state,
    create_memory_note_workflow_state,
    create_memory_text_import_workflow_state,
    create_memory_url_import_workflow_state,
    resume_memory_workflow,
)
from .retrieval import configure_persistent_retrieval
from .research_blackboard import ResearchBlackboard
from .research_control import HomogeneousForkConfig
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
from .v2_contracts import (
    CoreQuestion,
    ResearchArchitecture,
    ResearchArchitectureSettings,
    ResearchPlan,
    SupervisorV2Config,
)
from ..tools.content_extraction import content_extraction_config_from_config

from .vault_write_queue import VaultWriteQueue
from .vault_write_service import VaultWriteService
from .vault_writer import VaultWriter

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
    "homogeneous_fork_config_from_config",
    "load_config",
    "open_research_runtime",
    "research_architecture_settings_from_config",
    "shared_comparison_plan_from_config",
    "structured_report_enabled_from_config",
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
    extraction_config = content_extraction_config_from_config(config)

    search_tool = MockWebSearchTool() if mock_mode else WebSearchTool()
    browser_tool = (
        MockBrowserTool()
        if mock_mode
        else BrowserTool(extraction_config=extraction_config)
    )
    acquisition_raw = tools_config.get("evidence_acquisition", {})
    if not isinstance(acquisition_raw, Mapping):
        raise ValueError("tools.evidence_acquisition must be a mapping")
    acquisition_allowed = {
        "default_candidates",
        "default_sources",
        "max_sources",
        "max_chars_per_source",
    }
    acquisition_unknown = sorted(
        str(key) for key in acquisition_raw if key not in acquisition_allowed
    )
    if acquisition_unknown:
        raise ValueError(
            "unknown tools.evidence_acquisition settings: "
            + ", ".join(acquisition_unknown)
        )
    acquisition_tool = EvidenceAcquisitionTool(
        search_tool,
        browser_tool,
        **{
            key: acquisition_raw[key]
            for key in acquisition_allowed
            if key in acquisition_raw
        },
    )
    available: dict[str, Any] = {
        "web_search": search_tool,
        "browser": browser_tool,
        "acquire_evidence": acquisition_tool,
        "arxiv_reader": ArxivReaderTool(use_mock=mock_mode),
        "file_reader": FileReaderTool(),
        "code_sandbox": CodeSandboxTool(use_mock=mock_mode),
        "calculator": CalculatorTool(),
        "notepad": NotepadTool(),
    }
    base_names = tuple(name for name in available if name != "acquire_evidence")
    selected_names = list(base_names if enabled is None else enabled)
    if not isinstance(enabled, list):
        if enabled is not None:
            raise ValueError("tools.enabled must be a list of tool names")
    unknown = [name for name in selected_names if name not in available]
    if unknown:
        raise ValueError(f"unknown research tools: {', '.join(unknown)}")
    research = config.get("research", {})
    supervisor = research.get("supervisor_v2", {}) if isinstance(research, Mapping) else {}
    use_v2_acquisition = (
        isinstance(research, Mapping)
        and research.get("architecture") == ResearchArchitecture.SUPERVISOR_V2.value
        and isinstance(supervisor, Mapping)
        and bool(supervisor.get("enabled", False))
        and "web_search" in selected_names
        and "browser" in selected_names
    )
    if use_v2_acquisition:
        first_low_level = min(
            selected_names.index("web_search"),
            selected_names.index("browser"),
        )
        selected_names = [
            name for name in selected_names if name not in {"web_search", "browser"}
        ]
        selected_names.insert(first_low_level, "acquire_evidence")
    return [available[name] for name in selected_names]


def _research_config(config: dict[str, Any]) -> Mapping[str, Any]:
    research = config.get("research", {})
    if not isinstance(research, Mapping):
        raise ValueError("research configuration must be a mapping")
    return research


def research_architecture_settings_from_config(
    config: dict[str, Any],
) -> ResearchArchitectureSettings:
    """Strictly parse the V1/V2 rollout switch and bounded V2 settings."""
    research = _research_config(config)
    architecture_value = research.get("architecture", ResearchArchitecture.LEGACY.value)
    if not isinstance(architecture_value, str):
        raise ValueError(
            "research.architecture must be 'legacy' or 'supervisor_v2'"
        )
    try:
        architecture = ResearchArchitecture(architecture_value)
    except ValueError as exc:
        raise ValueError(
            "research.architecture must be 'legacy' or 'supervisor_v2'"
        ) from exc

    raw_supervisor = research.get("supervisor_v2", {})
    if not isinstance(raw_supervisor, Mapping):
        raise ValueError("research.supervisor_v2 must be a mapping")
    allowed = SupervisorV2Config.__dataclass_fields__.keys()
    unknown = sorted(str(key) for key in raw_supervisor if key not in allowed)
    if unknown:
        raise ValueError(
            "unknown research.supervisor_v2 settings: " + ", ".join(unknown)
        )
    supervisor = SupervisorV2Config(
        **{key: raw_supervisor[key] for key in allowed if key in raw_supervisor}
    )
    supervisor.validate()
    return ResearchArchitectureSettings(
        architecture=architecture,
        supervisor_v2=supervisor,
    )


def shared_comparison_plan_from_config(
    config: dict[str, Any],
) -> ResearchPlan | None:
    """Parse an opt-in fixed plan used only for architecture comparisons."""

    research = _research_config(config)
    raw = research.get("shared_comparison", {})
    if raw in ({}, None):
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("research.shared_comparison must be a mapping")
    allowed = {"enabled", "fixed_plan"}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "unknown research.shared_comparison settings: " + ", ".join(unknown)
        )
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("research.shared_comparison.enabled must be a boolean")
    if not enabled:
        return None
    fixed = raw.get("fixed_plan")
    if not isinstance(fixed, Mapping):
        raise ValueError(
            "research.shared_comparison.fixed_plan must be a mapping when enabled"
        )
    allowed_plan = {
        "brief_revision",
        "core_questions",
        "report_outline",
        "source_guidance",
        "work_hints",
    }
    unknown_plan = sorted(str(key) for key in fixed if key not in allowed_plan)
    if unknown_plan:
        raise ValueError(
            "unknown fixed comparison plan settings: " + ", ".join(unknown_plan)
        )
    raw_questions = fixed.get("core_questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise ValueError("fixed comparison plan requires core_questions")
    questions: list[CoreQuestion] = []
    allowed_question = {
        "description", "required", "priority", "origin", "verification",
        "requires_external_evidence",
    }
    for raw_question in raw_questions:
        if not isinstance(raw_question, Mapping):
            raise ValueError("fixed comparison core question must be a mapping")
        unknown_question = sorted(
            str(key) for key in raw_question if key not in allowed_question
        )
        if unknown_question:
            raise ValueError(
                "unknown fixed core question settings: "
                + ", ".join(unknown_question)
            )
        questions.append(CoreQuestion.create(
            str(raw_question.get("description") or ""),
            required=raw_question.get("required", True),
            priority=str(raw_question.get("priority") or "high"),
            origin=str(raw_question.get("origin") or "fixed_comparison"),
            verification=str(
                raw_question.get("verification") or "source-locatable evidence"
            ),
            requires_external_evidence=bool(
                raw_question.get("requires_external_evidence", True)
            ),
        ))
    return ResearchPlan.create(
        int(fixed.get("brief_revision", 0)),
        tuple(questions),
        report_outline=tuple(fixed.get("report_outline", ()) or ()),
        source_guidance=tuple(fixed.get("source_guidance", ()) or ()),
        work_hints=tuple(fixed.get("work_hints", ()) or ()),
    )


def homogeneous_fork_config_from_config(
    config: dict[str, Any],
) -> HomogeneousForkConfig:
    research = _research_config(config)
    raw = research.get("homogeneous_fork", {})
    if not isinstance(raw, Mapping):
        raise ValueError("research.homogeneous_fork must be a mapping")
    allowed = HomogeneousForkConfig.__dataclass_fields__.keys()
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "unknown research.homogeneous_fork settings: " + ", ".join(unknown)
        )
    settings = HomogeneousForkConfig(
        **{key: raw[key] for key in allowed if key in raw}
    )
    settings.validate()
    return settings


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


def _legacy_archive_root_from_config(config: dict[str, Any]) -> Path | None:
    research = _research_config(config)
    if "legacy_archive_root" not in research:
        return None
    configured = research["legacy_archive_root"]
    try:
        raw_path = os.fspath(configured)
    except TypeError as exc:
        raise ValueError(
            "research.legacy_archive_root must be a non-empty path-like string"
        ) from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(
            "research.legacy_archive_root must be a non-empty path-like string"
        )
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


def structured_report_enabled_from_config(config: dict[str, Any]) -> bool:
    """Return whether Legacy research should use the structured report path."""

    research = _research_config(config)
    structured = research.get("structured_report", {})
    if not isinstance(structured, Mapping):
        raise ValueError("research.structured_report must be a mapping")
    value = structured.get("enabled", False)
    if not isinstance(value, bool):
        raise ValueError("research.structured_report.enabled must be a boolean")
    return value


def _runtime_setting(
    config: dict[str, Any],
    key: str,
    default: float,
) -> float:
    section = config.get("runtime", {})
    if not isinstance(section, Mapping):
        raise ValueError("runtime configuration must be a mapping")
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"runtime.{key} must be a positive number")
    return float(value)


def _chat_db_path(config: dict[str, Any]) -> Path:
    section = config.get("chat", {})
    if not isinstance(section, Mapping):
        raise ValueError("chat configuration must be a mapping")
    configured = section.get("db_path", "data/chat.db")
    try:
        raw_path = os.fspath(configured)
    except TypeError as exc:
        raise ValueError("chat.db_path must be a non-empty path-like string") from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("chat.db_path must be a non-empty path-like string")
    path = Path(raw_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _retrieval_settings(config: dict[str, Any]) -> tuple[Path, float, bool, str, bool]:
    section = config.get("runtime", {})
    if not isinstance(section, Mapping):
        raise ValueError("runtime configuration must be a mapping")
    configured = section.get("retrieval_db_path", "data/retrieval.db")
    try:
        raw_path = os.fspath(configured)
    except TypeError as exc:
        raise ValueError("runtime.retrieval_db_path must be a non-empty path-like string") from exc
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("runtime.retrieval_db_path must be a non-empty path-like string")
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    interval = section.get("retrieval_reconciliation_seconds", 300)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        raise ValueError("runtime.retrieval_reconciliation_seconds must be a positive number")
    semantic_enabled = section.get("semantic_retrieval_enabled", False)
    local_files_only = section.get("semantic_local_files_only", True)
    if not isinstance(semantic_enabled, bool):
        raise ValueError("runtime.semantic_retrieval_enabled must be a boolean")
    if not isinstance(local_files_only, bool):
        raise ValueError("runtime.semantic_local_files_only must be a boolean")
    semantic_model = section.get(
        "semantic_embedding_model",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    if not isinstance(semantic_model, str) or not semantic_model.strip():
        raise ValueError("runtime.semantic_embedding_model must be a non-empty string")
    return path, float(interval), semantic_enabled, semantic_model.strip(), local_files_only


def _vault_scope(root: Path) -> str:
    canonical = os.path.normcase(str(root.resolve()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"vault-{digest}"


def _research_blackboard_path(
    write_db_path: str | os.PathLike[str],
) -> Path:
    """Keep concurrent blackboard writes outside the LangGraph checkpoint DB."""

    path = Path(write_db_path)
    return path.with_name(path.stem + ".blackboard.sqlite")


def _build_vault_write_service(
    *,
    config: dict[str, Any],
    memory_store: MarkdownMemoryStore,
    write_db_path: str | os.PathLike[str] | None = None,
    write_queue: VaultWriteQueue | None = None,
) -> VaultWriteService:
    expected_scope = _vault_scope(memory_store.root)
    if write_queue is None:
        queue = VaultWriteQueue(
            write_db_path if write_db_path is not None else _chat_db_path(config),
            vault_scope=expected_scope,
        )
    else:
        queue = write_queue
        if queue.vault_scope != expected_scope:
            raise ValueError("Vault write queue scope does not match the Memory Store")
    lease_seconds = _runtime_setting(config, "lease_seconds", 60)
    archive_root = _legacy_archive_root_from_config(config)
    writer = VaultWriter(
        memory_store.root,
        queue,
        job_lease_seconds=lease_seconds,
        legacy_archive_root=archive_root,
    )
    return VaultWriteService(
        memory_store,
        queue,
        writer,
        lease_seconds=lease_seconds,
        coordination_interval_seconds=_runtime_setting(
            config, "writer_coordination_interval_seconds", 0.1
        ),
        legacy_archive_root=archive_root,
        allow_legacy_copy_only=(
            archive_root is None and write_db_path is None and write_queue is None
        ),
    )


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
        research_architecture: ResearchArchitecture = ResearchArchitecture.LEGACY,
        supervisor_v2_config: SupervisorV2Config | None = None,
        vault_write_service: VaultWriteService | None = None,
        write_db_path: str | os.PathLike[str] | None = None,
        write_queue: VaultWriteQueue | None = None,
        research_blackboard: ResearchBlackboard | None = None,
        shared_comparison_plan: ResearchPlan | None = None,
        structured_report_enabled: bool = False,
        homogeneous_fork_config: HomogeneousForkConfig | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.tools = list(tools)
        self.memory_store = memory_store
        self.checkpointer = checkpointer
        self.limits = limits
        self.report_review_enabled = report_review_enabled
        self.research_architecture = research_architecture
        self.supervisor_v2_config = supervisor_v2_config or SupervisorV2Config()
        (
            retrieval_db,
            reconciliation_seconds,
            semantic_enabled,
            semantic_model,
            semantic_local_files_only,
        ) = _retrieval_settings(config)
        configure_persistent_retrieval(
            memory_store,
            retrieval_db,
            reconciliation_seconds=reconciliation_seconds,
            semantic_enabled=semantic_enabled,
            semantic_model=semantic_model,
            semantic_local_files_only=semantic_local_files_only,
        )
        self.vault_write_service = vault_write_service or _build_vault_write_service(
            config=config,
            memory_store=memory_store,
            write_db_path=write_db_path,
            write_queue=write_queue,
        )
        if self.vault_write_service.memory_store is not memory_store:
            raise ValueError("Vault write service must share the Runtime Memory Store")
        self.research_blackboard = research_blackboard or (
            ResearchBlackboard(
                _research_blackboard_path(write_db_path)
            )
            if write_db_path is not None else None
        )
        self.shared_comparison_plan = shared_comparison_plan
        self.structured_report_enabled = bool(
            structured_report_enabled or shared_comparison_plan is not None
        )
        self.homogeneous_fork_config = (
            homogeneous_fork_config or HomogeneousForkConfig()
        )
        self.proposal_ttl_seconds = _runtime_setting(
            config, "proposal_ttl_seconds", 86400
        )
        self.terminal_retention_seconds = _runtime_setting(
            config, "terminal_retention_seconds", 604800
        )
        self.lease_seconds = _runtime_setting(config, "lease_seconds", 60)
        self.sweep_interval_seconds = _runtime_setting(
            config, "sweep_interval_seconds", 5
        )
        self.graph = build_research_workflow(
            policy,
            self.tools,
            memory_store,
            checkpointer=checkpointer,
            report_review_enabled=report_review_enabled,
            research_architecture=research_architecture,
            supervisor_v2_config=self.supervisor_v2_config,
            vault_write_service=self.vault_write_service,
            research_blackboard=self.research_blackboard,
            shared_comparison_plan=self.shared_comparison_plan,
            structured_report_enabled=self.structured_report_enabled,
            homogeneous_fork_config=self.homogeneous_fork_config,
        )
        self.memory_note_graph = build_memory_note_workflow(
            memory_store,
            policy,
            checkpointer=checkpointer,
            vault_write_service=self.vault_write_service,
        )
        self.memory_import_graph = build_memory_import_workflow(
            memory_store,
            policy,
            checkpointer=checkpointer,
            vault_write_service=self.vault_write_service,
        )
        self.legacy_migration_graph = build_legacy_migration_workflow(
            memory_store,
            checkpointer=checkpointer,
            vault_write_service=self.vault_write_service,
        )

    @staticmethod
    def new_thread_id() -> str:
        return f"research-{uuid.uuid4().hex}"

    @staticmethod
    def new_workflow_id(workflow_type: str) -> str:
        return f"{workflow_type.replace('_', '-')}-{uuid.uuid4().hex}"

    @contextmanager
    def _research_file_scope(self, memory_id: str | None):
        """Authorize FileReader only for one explicitly selected managed Memory."""
        if memory_id is None or memory_id == LEGACY_MEMORY_ID:
            with file_reader_scope(None):
                yield
            return

        # Some embedding callers construct a deliberately minimal Runtime around a
        # custom graph. Missing store authority must disable FileReader rather than
        # widening access or breaking the unrelated graph operation.
        if getattr(self, "memory_store", None) is None:
            with file_reader_scope(None):
                yield
            return

        descriptor = self.get_memory(memory_id)
        vault_root = Path(self.memory_store.root).resolve(strict=True)
        lexical_root = vault_root.joinpath(*Path(descriptor.relative_path).parts)
        memory_root = lexical_root.resolve(strict=True)
        if memory_root == vault_root or not memory_root.is_relative_to(vault_root):
            raise ValueError("selected Memory file scope escapes the configured Vault")
        if memory_root != lexical_root:
            raise ValueError("selected Memory file scope cannot traverse a linked path")
        with file_reader_scope({"memory": memory_root}):
            yield

    async def start(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        memory_id: str | None = None,
        session_id: str | None = None,
        expires_at: float | None = None,
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
            with self._research_file_scope(memory_id):
                result = await self.graph.ainvoke(
                    create_research_workflow_state(
                        question,
                        identity,
                        self.limits,
                        memory_id=memory_id,
                        session_id=session_id,
                        expires_at=expires_at,
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
        *,
        session_id: str | None = None,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        state = await self.get_state(thread_id)
        state_session_id = state.get("session_id")
        state_memory_id = state.get("memory_id")
        if session_id is not None and state_session_id != session_id:
            raise ValueError("research workflow does not belong to this session")
        if memory_id is not None and state_memory_id != memory_id:
            raise ValueError("research workflow does not belong to this Memory")
        expires_at = state.get("expires_at")
        if (
            action != "expire"
            and expires_at is not None
            and time.time() >= float(expires_at)
        ):
            raise TimeoutError("research confirmation has expired")
        with _memory_trace("paperpilot.research.review", state_memory_id) as observation:
            with self._research_file_scope(state_memory_id):
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

    async def confirm_research_start(
        self,
        thread_id: str,
        *,
        session_id: str | None = None,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist only the confirmation super-step before background execution."""
        state = await self.get_state(thread_id)
        if session_id is not None and state.get("session_id") != session_id:
            raise ValueError("research workflow does not belong to this session")
        if memory_id is not None and state.get("memory_id") != memory_id:
            raise ValueError("research workflow does not belong to this Memory")
        expires_at = state.get("expires_at")
        if expires_at is not None and time.time() >= float(expires_at):
            raise TimeoutError("research confirmation has expired")
        config = {"configurable": {"thread_id": thread_id}}
        with _memory_trace(
            "paperpilot.research.confirm", state.get("memory_id")
        ) as observation:
            with self._research_file_scope(state.get("memory_id")):
                result = await self.graph.ainvoke(
                    Command(resume={"action": "confirm"}),
                    config=config,
                    interrupt_after=["review_brief"],
                )
            if result.get("confirmed") is not True:
                raise RuntimeError("research confirmation super-step was not persisted")
            observation.add_output(
                {
                    "memory_id": state.get("memory_id"),
                    "thread_id": thread_id,
                    "action": "confirm",
                }
            )
            return result

    async def bind_research_memory(
        self,
        thread_id: str,
        memory_id: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Attach a newly created empty Memory to one paused research proposal."""

        descriptor = self.get_memory(memory_id)
        snapshot = await self.get_snapshot(thread_id)
        state = dict(snapshot.values)
        if session_id is not None and state.get("session_id") != session_id:
            raise ValueError("research workflow does not belong to this session")
        current_memory_id = state.get("memory_id")
        if current_memory_id is not None:
            if current_memory_id != descriptor.memory_id:
                raise ValueError("research workflow is already bound to another Memory")
            return state
        if state.get("workflow_status") != "waiting_confirmation":
            raise ValueError("research Memory can only be bound while awaiting confirmation")
        if len(getattr(snapshot, "interrupts", ())) != 1:
            raise ValueError("research workflow is not paused for confirmation")
        brief = state.get("brief")
        if not isinstance(brief, ResearchBrief):
            raise TypeError("research workflow has no bindable brief")

        rebound_brief = replace(
            brief,
            memory_id=descriptor.memory_id,
            memory_paths=(),
            known_information=(),
            research_gaps=brief.directions,
        )
        config = {"configurable": {"thread_id": thread_id}}
        with _memory_trace(
            "paperpilot.research.bind_memory", descriptor.memory_id
        ) as observation:
            await self.graph.aupdate_state(
                config,
                {
                    "memory_id": descriptor.memory_id,
                    "brief": rebound_brief,
                    "retrieved_memory": [],
                },
                as_node="draft_brief",
            )
            with self._research_file_scope(descriptor.memory_id):
                await self.graph.ainvoke(None, config=config)
            rebound = await self.get_snapshot(thread_id)
            values = dict(rebound.values)
            if (
                values.get("memory_id") != descriptor.memory_id
                or len(getattr(rebound, "interrupts", ())) != 1
            ):
                raise RuntimeError("research Memory binding did not preserve confirmation")
            observation.add_output(
                {
                    "memory_id": descriptor.memory_id,
                    "thread_id": thread_id,
                }
            )
            return values

    async def stream_confirm(self, thread_id: str) -> AsyncIterator[Any]:
        """Confirm one paused run while preserving its scoped FileReader context."""
        state = await self.get_state(thread_id)
        memory_id = state.get("memory_id")
        config = {"configurable": {"thread_id": thread_id}}
        with _memory_trace("paperpilot.research.review", memory_id) as observation:
            with self._research_file_scope(memory_id):
                async for update in self.graph.astream(
                    Command(resume={"action": "confirm"}),
                    config=config,
                    stream_mode="updates",
                    subgraphs=True,
                ):
                    yield update
            final_state = await self.get_state(thread_id)
            workflow_result = final_state.get("workflow_result")
            manifest = getattr(workflow_result, "memory_manifest", None)
            observation.add_output(
                {
                    "memory_id": memory_id,
                    "thread_id": thread_id,
                    "action": "confirm",
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

    async def continue_research(self, thread_id: str) -> dict[str, Any]:
        """Continue a non-interrupted checkpoint without replacing its State."""
        state = await self.get_state(thread_id)
        memory_id = state.get("memory_id")
        with _memory_trace("paperpilot.research.continue", memory_id):
            with self._research_file_scope(memory_id):
                return await self.graph.ainvoke(
                    None,
                    config={"configurable": {"thread_id": thread_id}},
                )

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

    async def get_snapshot(self, thread_id: str) -> Any:
        """Return the authoritative LangGraph snapshot for recovery/status views."""
        return await self.graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )

    async def mark_research_failed(self, thread_id: str, code: str) -> None:
        """Persist a bounded failure marker in State after an application failure."""
        await self.graph.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {"workflow_status": "failed", "failure_code": code},
            as_node="postprocess_report",
        )

    async def mark_workflow_failed(
        self, workflow_type: str, thread_id: str, code: str
    ) -> None:
        """Terminally mark a checkpointed product workflow after adapter failure."""
        if workflow_type == "research":
            await self.mark_research_failed(thread_id, code)
            return
        graph = self.workflow_graph(workflow_type)
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        values = dict(snapshot.values)
        await graph.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "workflow_status": "failed",
                "result": {
                    "status": "failed",
                    "workflow_type": workflow_type,
                    "thread_id": thread_id,
                    "session_id": values.get("session_id"),
                    "memory_id": values.get("memory_id"),
                    "error": code,
                },
            },
            as_node="finish",
        )

    async def mark_workflow_cancelled(
        self, workflow_type: str, thread_id: str
    ) -> None:
        """Terminally cancel a running workflow before adapter-owned deletion."""
        if workflow_type == "research":
            await self.graph.aupdate_state(
                {"configurable": {"thread_id": thread_id}},
                {"workflow_status": "cancelled"},
                as_node="postprocess_report",
            )
            return
        graph = self.workflow_graph(workflow_type)
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        values = dict(snapshot.values)
        await graph.aupdate_state(
            {"configurable": {"thread_id": thread_id}},
            {
                "workflow_status": "cancelled",
                "decision": "cancel",
                "result": {
                    "status": "cancelled",
                    "workflow_type": workflow_type,
                    "thread_id": thread_id,
                    "session_id": values.get("session_id"),
                    "memory_id": values.get("memory_id"),
                },
            },
            as_node="finish",
        )

    async def delete_workflow(self, thread_id: str) -> None:
        """Delete all checkpoints for one product workflow thread."""
        delete = getattr(self.checkpointer, "adelete_thread", None)
        if delete is None:
            return
        await delete(thread_id)

    def workflow_graph(self, workflow_type: str) -> Any:
        graphs = {
            "research": self.graph,
            "memory_note": self.memory_note_graph,
            "memory_import": self.memory_import_graph,
            "legacy_migration": self.legacy_migration_graph,
        }
        try:
            return graphs[workflow_type]
        except KeyError as exc:
            raise ValueError(f"unsupported workflow_type: {workflow_type}") from exc

    async def get_workflow_snapshot(self, workflow_type: str, thread_id: str) -> Any:
        return await self.workflow_graph(workflow_type).aget_state(
            {"configurable": {"thread_id": thread_id}}
        )

    async def resume_memory_operation(
        self,
        workflow_type: str,
        thread_id: str,
        decision: Mapping[str, Any],
    ) -> dict[str, Any]:
        if workflow_type not in {
            "memory_note", "memory_import", "legacy_migration"
        }:
            raise ValueError("workflow is not a Memory confirmation operation")
        return await resume_memory_workflow(
            self.workflow_graph(workflow_type),
            thread_id=thread_id,
            decision=decision,
        )

    async def continue_workflow(
        self,
        workflow_type: str,
        thread_id: str,
    ) -> dict[str, Any]:
        if workflow_type == "research":
            return await self.continue_research(thread_id)
        return await continue_memory_workflow(
            self.workflow_graph(workflow_type), thread_id=thread_id
        )

    async def start_memory_note_workflow(
        self,
        *,
        session_id: str,
        memory_id: str,
        question: str,
        thread_id: str | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        workflow_id = thread_id or self.new_workflow_id("memory_note")
        return await self.memory_note_graph.ainvoke(
            create_memory_note_workflow_state(
                thread_id=workflow_id,
                session_id=session_id,
                memory_id=memory_id,
                question=question,
                expires_at=expires_at,
                ttl_seconds=self.proposal_ttl_seconds,
            ),
            config={"configurable": {"thread_id": workflow_id}},
        )

    async def start_memory_import_workflow(
        self,
        *,
        session_id: str,
        memory_id: str,
        source: Mapping[str, Any],
        thread_id: str | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        workflow_id = thread_id or self.new_workflow_id("memory_import")
        common = {
            "thread_id": workflow_id,
            "session_id": session_id,
            "memory_id": memory_id,
            "expires_at": expires_at,
            "ttl_seconds": self.proposal_ttl_seconds,
        }
        kind = source.get("kind")
        if kind == "file":
            state = create_memory_file_import_workflow_state(
                **common,
                file_name=str(source.get("file_name") or ""),
                content=source.get("content", b""),
            )
        elif kind == "text":
            state = create_memory_text_import_workflow_state(
                **common,
                title=str(source.get("title") or ""),
                text=str(source.get("text") or ""),
            )
        elif kind == "url":
            state = create_memory_url_import_workflow_state(
                **common,
                url=str(source.get("url") or ""),
            )
        else:
            raise ValueError("unsupported Memory import source")
        return await self.memory_import_graph.ainvoke(
            state,
            config={"configurable": {"thread_id": workflow_id}},
        )

    async def start_legacy_migration_workflow(
        self,
        *,
        session_id: str,
        title: str,
        target_memory_id: str,
        thread_id: str | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        workflow_id = thread_id or self.new_workflow_id("legacy_migration")
        return await self.legacy_migration_graph.ainvoke(
            create_legacy_migration_workflow_state(
                thread_id=workflow_id,
                session_id=session_id,
                title=title,
                target_memory_id=target_memory_id,
                expires_at=expires_at,
                ttl_seconds=self.proposal_ttl_seconds,
            ),
            config={"configurable": {"thread_id": workflow_id}},
        )

    def read_memory(self, relative_path: str) -> str:
        service = getattr(self, "vault_write_service", None)
        resolved = (
            service.resolve_memory_path(relative_path)
            if service is not None
            else relative_path
        )
        return self.memory_store.read_text(resolved)

    def create_memory(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> MemoryDescriptor:
        service = getattr(self, "vault_write_service", None)
        if service is None:
            return self.memory_store.create_memory(title, memory_id=memory_id)
        return service.create_memory(title, memory_id=memory_id)

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
            service = getattr(self, "vault_write_service", None)
            proposal = (
                service.prepare_legacy_memory_migration(title, memory_id)
                if service is not None
                else self.memory_store.prepare_legacy_memory_migration(title, memory_id)
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
            service = getattr(self, "vault_write_service", None)
            descriptor = (
                service.commit_legacy_memory_migration(proposal)
                if service is not None
                else self.memory_store.commit_legacy_memory_migration(proposal)
            )
            observation.add_output(
                {
                    "status": "committed",
                    "source_memory_id": LEGACY_MEMORY_ID,
                    "memory_id": descriptor.memory_id,
                    "home_path": f"{descriptor.relative_path}Home.md",
                }
            )
            return descriptor

    def prepare_legacy_archive_cleanup(self, migration_id: str) -> dict[str, object]:
        return self.vault_write_service.prepare_legacy_archive_cleanup(migration_id)

    def delete_legacy_archive(
        self,
        migration_id: str,
        *,
        confirmation_token: str,
    ) -> dict[str, object]:
        return self.vault_write_service.delete_legacy_archive(
            migration_id,
            confirmation_token=confirmation_token,
        )

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
            service = getattr(self, "vault_write_service", None)
            result = (
                service.commit_memory_note(proposal)
                if service is not None
                else self.memory_store.commit_memory_note(proposal)
            )
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
            service = getattr(self, "vault_write_service", None)
            result = (
                service.commit_memory_import(proposal)
                if service is not None
                else self.memory_store.commit_memory_import(proposal)
            )
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
    write_db_path: str | os.PathLike[str] | None = None,
    write_queue: VaultWriteQueue | None = None,
    vault_write_service: VaultWriteService | None = None,
    research_blackboard: ResearchBlackboard | None = None,
    shared_comparison_plan: ResearchPlan | None = None,
    structured_report_enabled: bool | None = None,
    homogeneous_fork_config: HomogeneousForkConfig | None = None,
) -> ResearchRuntime:
    """Construct the single production Research Workflow dependency graph."""
    effective_config = config if config is not None else load_config(config_path)
    architecture_settings = research_architecture_settings_from_config(effective_config)
    if (
        architecture_settings.architecture is ResearchArchitecture.SUPERVISOR_V2
        and not architecture_settings.supervisor_v2.enabled
    ):
        raise ValueError(
            "research.architecture=supervisor_v2 requires "
            "research.supervisor_v2.enabled=true"
        )
    effective_store = (
        memory_store
        if memory_store is not None
        else MarkdownMemoryStore(vault_root_from_config(effective_config))
    )
    effective_shared_plan = (
        shared_comparison_plan
        if shared_comparison_plan is not None
        else shared_comparison_plan_from_config(effective_config)
    )
    effective_fork_config = (
        homogeneous_fork_config
        if homogeneous_fork_config is not None
        else homogeneous_fork_config_from_config(effective_config)
    )
    effective_structured_report = (
        structured_report_enabled
        if structured_report_enabled is not None
        else structured_report_enabled_from_config(effective_config)
    )
    return ResearchRuntime(
        config=effective_config,
        policy=policy if policy is not None else _build_policy(effective_config),
        tools=list(tools) if tools is not None else build_research_tools(effective_config),
        memory_store=effective_store,
        checkpointer=(
            checkpointer if checkpointer is not None else paperpilot_in_memory_saver()
        ),
        limits=limits_from_config(effective_config),
        report_review_enabled=_report_review_enabled(effective_config),
        research_architecture=architecture_settings.architecture,
        supervisor_v2_config=architecture_settings.supervisor_v2,
        vault_write_service=vault_write_service,
        write_db_path=write_db_path,
        write_queue=write_queue,
        research_blackboard=research_blackboard,
        shared_comparison_plan=effective_shared_plan,
        structured_report_enabled=effective_structured_report,
        homogeneous_fork_config=effective_fork_config,
    )


@asynccontextmanager
async def open_research_runtime(
    checkpoint_db_path: str | os.PathLike[str],
    config: dict[str, Any] | None = None,
    *,
    config_path: str | os.PathLike[str] | None = None,
    policy: Any | None = None,
    tools: Iterable[Any] | None = None,
    memory_store: MarkdownMemoryStore | None = None,
) -> AsyncIterator[ResearchRuntime]:
    """Own one persistent SQLite saver for a product process lifecycle."""
    path = Path(checkpoint_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(os.fspath(path)) as saver:
        saver.serde = paperpilot_checkpoint_serializer()
        await saver.setup()
        runtime = build_research_runtime(
            config,
            config_path=config_path,
            policy=policy,
            tools=tools,
            memory_store=memory_store,
            checkpointer=saver,
            write_db_path=path,
        )
        try:
            await asyncio.to_thread(runtime.vault_write_service.startup_recover)
            yield runtime
        finally:
            await runtime.close(shutdown=True)
