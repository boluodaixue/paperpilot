"""Contracts for product conversation routing, independent of Research Core."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConversationAction(str, Enum):
    REPLY = "reply"
    CLARIFY = "clarify"
    MEMORY_ANSWER = "memory_answer"
    QUICK_SEARCH = "quick_search"
    PROPOSE_RESEARCH = "propose_research"
    PROPOSE_MEMORY_WRITE = "propose_memory_write"


class ActionOverride(str, Enum):
    AUTO = "auto"
    MEMORY_ONLY = "memory_only"
    QUICK_SEARCH = "quick_search"
    DEEP_RESEARCH = "deep_research"
    SAVE_TO_MEMORY = "save_to_memory"


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("ConversationMessage.role must be user or assistant")
        if not self.content.strip():
            raise ValueError("ConversationMessage.content cannot be empty")


@dataclass(frozen=True)
class MemorySelection:
    memory_id: str
    title: str
    read_only: bool = False

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValueError("MemorySelection.memory_id cannot be empty")
        if not self.title.strip():
            raise ValueError("MemorySelection.title cannot be empty")


@dataclass(frozen=True)
class ConversationRequest:
    message: str
    recent_messages: tuple[ConversationMessage, ...] = ()
    selected_memory: MemorySelection | None = None
    explicit_action: ActionOverride = ActionOverride.AUTO

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("ConversationRequest.message cannot be empty")


@dataclass(frozen=True)
class ConversationDecision:
    action: ConversationAction
    confidence: float
    response: str = ""
    query: str = ""
    reason_code: str = ""
    requires_memory: bool = False
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("ConversationDecision.confidence must be between 0 and 1")
        if self.action in {ConversationAction.REPLY, ConversationAction.CLARIFY}:
            if not self.response.strip():
                raise ValueError(f"{self.action.value} requires a response")
        elif not self.query.strip():
            raise ValueError(f"{self.action.value} requires a query")


__all__ = [
    "ActionOverride",
    "ConversationAction",
    "ConversationDecision",
    "ConversationMessage",
    "ConversationRequest",
    "MemorySelection",
]
