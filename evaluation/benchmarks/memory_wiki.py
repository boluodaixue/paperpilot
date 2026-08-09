"""Fixed offline evaluation for the LLM Wiki + Obsidian memory loop."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable

from langgraph.checkpoint.memory import InMemorySaver

from evaluation.report import EvaluationReport
from src.research.memory import MarkdownMemoryStore, MemoryWriteConflictError
from src.research.memory_dialogue import answer_memory, propose_memory_note
from src.research.models import AgentLimits, ExecutionIdentity, ResearchWorkflowResult
from src.research.retrieval import MarkdownMemoryIndex
from src.research.workflow import (
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)


_MEMORY_ID = "M-offline"
_OTHER_MEMORY_ID = "M-other"
_QUERY = "What does catalysttoken establish?"
_WIKILINK = re.compile(r"\[\[[^\]\r\n]+\]\]")


def _write_note(
    root: Path,
    memory_id: str,
    *,
    name: str,
    title: str,
    body: str,
) -> str:
    relative_path = f"Memories/{memory_id}/notes/{name}.md"
    (root / relative_path).write_text(
        (
            "---\n"
            f'id: "Note-{name}"\n'
            'type: "note"\n'
            f'memory_id: "{memory_id}"\n'
            f'title: "{title}"\n'
            'created_at: "2026-08-28T00:00:00+08:00"\n'
            'updated_at: "2026-08-28T00:00:00+08:00"\n'
            'origin: "user"\n'
            'status: "confirmed"\n'
            "tags:\n  - paperpilot\n"
            "---\n\n"
            f"# {title}\n\n{body}\n"
        ),
        encoding="utf-8",
    )
    return relative_path


def _vault_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fixture(root: Path) -> tuple[MarkdownMemoryStore, str, str]:
    store = MarkdownMemoryStore(root)
    store.create_memory("Offline Memory", memory_id=_MEMORY_ID)
    store.create_memory("Other Memory", memory_id=_OTHER_MEMORY_ID)
    selected_path = _write_note(
        root,
        _MEMORY_ID,
        name="Catalyst-source",
        title="Catalyst source",
        body="Catalysttoken establishes the selected Memory baseline.",
    )
    other_path = _write_note(
        root,
        _OTHER_MEMORY_ID,
        name="Catalyst-private",
        title="Private catalyst source",
        body="Catalysttoken appears here but belongs to another Memory.",
    )
    return store, selected_path, other_path


class _OfflinePolicy:
    """One deterministic policy fixture for dialogue, notes, and research."""

    def __init__(self) -> None:
        self.calls = 0
        self.answer_calls = 0
        self.research_calls = 0

    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        system = str(messages[0].get("content") or "")

        if "Answer only from the supplied selected-Memory notes" in system:
            self.answer_calls += 1
            context = json.loads(
                str(messages[-1]["content"]).split("MEMORY_CONTEXT_JSON:\n", 1)[1]
            )
            selected_path = str(context["hits"][0]["path"])
            return {
                "content": json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The selected note establishes the baseline.",
                                "source_paths": [selected_path],
                            },
                            {
                                "text": "A forged cross-Memory claim.",
                                "source_paths": [
                                    f"Memories/{_OTHER_MEMORY_ID}/notes/Forged.md"
                                ],
                            },
                            {
                                "text": "An uncited claim.",
                                "source_paths": [],
                            },
                            {
                                "text": "A model supplied [[forged-link]].",
                                "source_paths": [selected_path],
                            },
                        ],
                        "insufficient_evidence": [
                            "The selected Memory does not establish a second claim."
                        ],
                    }
                )
            }

        if "Create a complete Markdown note" in system:
            contract_text = str(messages[-1]["content"]).split(
                "FIXED_NOTE_CONTRACT_JSON:\n", 1
            )[1].split("\n\nMEMORY_ANSWER:", 1)[0]
            contract = json.loads(contract_text)
            fixed = contract["frontmatter"]
            title = "Offline grounded note"
            source_lines = "\n".join(
                f"- [[{path[:-3]}]]"
                for path in contract["allowed_source_paths"]
            )
            markdown = (
                "---\n"
                f'id: {json.dumps(fixed["id"])}\n'
                f'type: {json.dumps(fixed["type"])}\n'
                f'memory_id: {json.dumps(fixed["memory_id"])}\n'
                f'title: {json.dumps(title)}\n'
                f'created_at: {json.dumps(fixed["created_at"])}\n'
                f'updated_at: {json.dumps(fixed["updated_at"])}\n'
                f'origin: {json.dumps(fixed["origin"])}\n'
                f'status: {json.dumps(fixed["status"])}\n'
                "tags:\n  - paperpilot\n"
                "---\n\n"
                f"# {title}\n\n"
                "The selected note establishes the baseline.\n\n"
                f"## Sources\n\n{source_lines}\n"
            )
            return {"content": json.dumps({"markdown": markdown})}

        if "before research begins" in system:
            return {
                "content": json.dumps(
                    {
                        "objective": "Extend the selected Memory baseline",
                        "scope": ["selected Memory"],
                        "directions": ["Find one new source"],
                        "constraints": ["Keep sources locatable"],
                        "expected_output": "A sourced Markdown report",
                        "known_information": [
                            "The selected Memory already establishes the baseline."
                        ],
                        "research_gaps": ["One new source is still needed."],
                        "memory_id": "M-forged",
                        "memory_paths": ["Memories/M-forged/notes/Forged.md"],
                    }
                ),
                "tool_calls": [],
            }

        if "You are a PaperPilot Research Agent" in system:
            self.research_calls += 1
            if messages[-1].get("role") == "tool":
                return {
                    "content": json.dumps(
                        {
                            "status": "completed",
                            "summary": "A fixed source extends the baseline.",
                            "findings": [
                                "The new source fills the selected Memory gap."
                            ],
                            "unresolved": [],
                        }
                    ),
                    "tool_calls": [],
                }
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "offline-search",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": "{}",
                        },
                    }
                ],
            }

        raise AssertionError("offline policy received an unexpected prompt")


class _OfflineTool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Fixed offline evidence source",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "Fixed offline source",
                    "url": "https://example.invalid/offline-source",
                    "snippet": "The fixed source fills the selected Memory gap.",
                }
            ]
        }


async def _retrieval_hit_case() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="paperpilot-memory-eval-") as directory:
        root = Path(directory)
        store, selected_path, other_path = _fixture(root)
        hits = MarkdownMemoryIndex(store).search(_MEMORY_ID, "catalysttoken")
        actual_paths = tuple(hit.relative_path for hit in hits)
        return {
            "expected_selected_hit": actual_paths == (selected_path,),
            "other_memory_excluded": other_path not in actual_paths,
            "index_not_persisted": not list(root.rglob("*index*")),
        }


async def _citation_completeness_case() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="paperpilot-memory-eval-") as directory:
        root = Path(directory)
        store, selected_path, _ = _fixture(root)
        policy = _OfflinePolicy()
        answer = await answer_memory(store, policy, _MEMORY_ID, _QUERY)
        grounded = answer.markdown.split("\n\n## Insufficient evidence", 1)[0]
        claim_lines = [line for line in grounded.splitlines() if line.startswith("- ")]
        allowed_links = {citation.wikilink for citation in answer.citations}
        emitted_links = set(_WIKILINK.findall(grounded))
        return {
            "one_valid_citation": tuple(
                citation.relative_path for citation in answer.citations
            )
            == (selected_path,),
            "every_retained_claim_is_cited": bool(claim_lines)
            and all(any(link in line for link in allowed_links) for line in claim_lines),
            "all_emitted_links_are_validated": emitted_links == allowed_links,
            "forged_paths_filtered": _OTHER_MEMORY_ID not in answer.markdown,
            "uncited_and_model_links_filtered": "uncited claim" not in answer.markdown
            and "forged-link" not in answer.markdown,
        }


async def _unsupported_refusal_case() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="paperpilot-memory-eval-") as directory:
        root = Path(directory)
        store, _, _ = _fixture(root)
        policy = _OfflinePolicy()
        before = _vault_files(root)
        answer = await answer_memory(
            store,
            policy,
            _MEMORY_ID,
            "Zygomorphic spectroheliograph",
        )
        return {
            "policy_not_called": policy.calls == 0,
            "no_citations": answer.citations == (),
            "explicit_refusal": bool(answer.insufficient_evidence)
            and "Insufficient evidence" in answer.markdown,
            "vault_unchanged": _vault_files(root) == before,
        }


async def _controlled_write_case() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="paperpilot-memory-eval-") as directory:
        root = Path(directory)
        store, selected_path, _ = _fixture(root)
        policy = _OfflinePolicy()
        answer = await answer_memory(store, policy, _MEMORY_ID, _QUERY)
        before = _vault_files(root)
        proposal = await propose_memory_note(store, policy, answer)
        proposal_is_read_only = _vault_files(root) == before
        source_before = before[selected_path]
        result = store.commit_memory_note(proposal)
        after = _vault_files(root)
        new_files = set(after).difference(before)
        changed_files = {
            path for path in set(after).intersection(before) if after[path] != before[path]
        }

        second_answer = await answer_memory(store, policy, _MEMORY_ID, _QUERY)
        conflicting = await propose_memory_note(store, policy, second_answer)
        home = root / conflicting.home_path
        home.write_text(
            home.read_text(encoding="utf-8") + "\nExternal Obsidian edit.\n",
            encoding="utf-8",
        )
        external_home = home.read_bytes()
        conflict_rejected = False
        try:
            store.commit_memory_note(conflicting)
        except MemoryWriteConflictError:
            conflict_rejected = True

        return {
            "proposal_is_read_only": proposal_is_read_only,
            "confirmed_note_is_only_new_file": new_files == {proposal.target_path},
            "confirmed_home_is_only_changed_file": changed_files == {
                proposal.home_path
            },
            "commit_result_matches_paths": result
            == {
                "memory_id": proposal.memory_id,
                "target_path": proposal.target_path,
                "home_path": proposal.home_path,
                "wikilink": proposal.wikilink,
            },
            "source_preserved": after[selected_path] == source_before,
            "external_edit_conflict_rejected": conflict_rejected
            and home.read_bytes() == external_home
            and not (root / conflicting.target_path).exists(),
        }


async def _continued_research_case() -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="paperpilot-memory-eval-") as directory:
        root = Path(directory)
        store, selected_path, other_path = _fixture(root)
        selected_before = (root / selected_path).read_bytes()
        policy = _OfflinePolicy()
        tool = _OfflineTool()
        graph = build_research_workflow(
            policy,
            [tool],
            store,
            checkpointer=InMemorySaver(),
        )
        thread_id = "memory-wiki-offline"
        identity = ExecutionIdentity(thread_id, None, thread_id, 0)
        paused = await graph.ainvoke(
            create_research_workflow_state(
                "Continue catalysttoken research",
                identity,
                AgentLimits(max_fork_depth=1, max_children=2),
                memory_id=_MEMORY_ID,
            ),
            config={"configurable": {"thread_id": thread_id}},
        )
        interrupts = paused.get("__interrupt__")
        if not interrupts:
            raise RuntimeError("offline workflow did not pause for brief confirmation")
        brief_preview = interrupts[0].value["brief"]
        no_tools_before_confirmation = tool.calls == 0
        final = await resume_research_workflow(
            graph,
            thread_id=thread_id,
            action="confirm",
        )
        workflow_result = final.get("workflow_result")
        if not isinstance(workflow_result, ResearchWorkflowResult):
            raise RuntimeError("offline workflow did not return a structured result")
        manifest = workflow_result.memory_manifest
        written_paths = (
            manifest.report_path,
            *manifest.evidence_paths,
            *manifest.source_paths,
        )
        prefix = f"Memories/{_MEMORY_ID}/"
        return {
            "brief_uses_exact_selected_file": tuple(brief_preview["memory_paths"])
            == (selected_path,),
            "brief_excludes_other_memory": other_path
            not in tuple(brief_preview["memory_paths"]),
            "brief_exposes_known_and_gap": bool(brief_preview["known_information"])
            and bool(brief_preview["research_gaps"]),
            "confirmation_precedes_tools": no_tools_before_confirmation,
            "fixed_tool_called_once": tool.calls == 1,
            "result_keeps_memory_id": workflow_result.memory_id == _MEMORY_ID,
            "all_results_written_to_same_memory": bool(written_paths)
            and all(path.startswith(prefix) for path in written_paths)
            and all((root / path).is_file() for path in written_paths),
            "prior_note_preserved": (root / selected_path).read_bytes()
            == selected_before,
        }


_Case = Callable[[], Awaitable[dict[str, bool]]]
_CASES: tuple[tuple[str, _Case], ...] = (
    ("retrieval_hit", _retrieval_hit_case),
    ("citation_completeness", _citation_completeness_case),
    ("unsupported_refusal", _unsupported_refusal_case),
    ("controlled_write", _controlled_write_case),
    ("continued_research", _continued_research_case),
)


async def _evaluate_memory_wiki() -> EvaluationReport:
    report = EvaluationReport(
        name="MemoryWiki_Offline_Evaluation",
        num_questions=len(_CASES),
    )
    for case_id, case in _CASES:
        try:
            checks = await case()
            passed = bool(checks) and all(checks.values())
            detail: dict[str, Any] = {
                "question_id": case_id,
                "case_id": case_id,
                "passed": passed,
                "checks": checks,
            }
        except Exception as exc:
            detail = {
                "question_id": case_id,
                "case_id": case_id,
                "passed": False,
                "checks": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        report.add_detail(detail)

    passed_count = sum(detail["passed"] is True for detail in report.details)
    failed_count = len(report.details) - passed_count
    report.set_summary(
        {
            "num_passed": passed_count,
            "num_failed": failed_count,
            "pass_rate": passed_count / len(report.details) if report.details else 0.0,
            "all_passed": failed_count == 0,
        }
    )
    return report


def evaluate_memory_wiki() -> EvaluationReport:
    """Run all five fixed cases without a model, network, or persistent Vault."""
    return asyncio.run(_evaluate_memory_wiki())


__all__ = ["evaluate_memory_wiki"]
