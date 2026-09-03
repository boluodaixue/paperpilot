from __future__ import annotations

from src.research.checkpoint_serde import (
    paperpilot_checkpoint_serializer,
    paperpilot_in_memory_saver,
)
from src.research.models import ExecutionIdentity, ResearchBrief


def test_paperpilot_checkpoint_serializer_round_trips_owned_state() -> None:
    value = {
        "brief": ResearchBrief(
            question="Question",
            objective="Objective",
            scope=(),
            directions=("Direction",),
            constraints=(),
            expected_output="Report",
        ),
        "identity": ExecutionIdentity("root", None, "root", 0),
    }
    serializer = paperpilot_checkpoint_serializer()

    encoded = serializer.dumps_typed(value)

    decoded = serializer.loads_typed(encoded)

    assert isinstance(decoded["brief"], ResearchBrief)
    assert decoded["brief"].question == value["brief"].question
    assert tuple(decoded["brief"].directions) == value["brief"].directions
    assert decoded["identity"] == value["identity"]


def test_paperpilot_in_memory_saver_uses_explicit_serializer() -> None:
    saver = paperpilot_in_memory_saver()

    assert saver.serde.loads_typed(
        saver.serde.dumps_typed(ExecutionIdentity("root", None, "root", 0))
    ) == ExecutionIdentity("root", None, "root", 0)
