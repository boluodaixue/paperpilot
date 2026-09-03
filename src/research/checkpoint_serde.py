"""Explicit checkpoint serialization allowlist for PaperPilot-owned state."""

from __future__ import annotations

import inspect
from types import ModuleType

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def _owned_types(module: ModuleType) -> tuple[type, ...]:
    return tuple(
        value
        for value in vars(module).values()
        if inspect.isclass(value) and value.__module__ == module.__name__
    )


def paperpilot_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only the application types intentionally stored in checkpoints."""

    from . import models, research_control, v2_contracts

    allowed = (
        *_owned_types(models),
        *_owned_types(research_control),
        *_owned_types(v2_contracts),
    )
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def paperpilot_in_memory_saver() -> InMemorySaver:
    return InMemorySaver(serde=paperpilot_checkpoint_serializer())


__all__ = ["paperpilot_checkpoint_serializer", "paperpilot_in_memory_saver"]
