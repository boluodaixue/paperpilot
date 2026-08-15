from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from scripts.prepare_semantic_model import prepare_semantic_model


def test_prepare_semantic_model_checks_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    class Model:
        def __init__(self, model_id: str, *, local_files_only: bool) -> None:
            observed["model_id"] = model_id
            observed["local_files_only"] = local_files_only

        def encode(self, texts, **kwargs):
            observed["texts"] = texts
            observed["encode"] = kwargs
            return [[3.0, 4.0]]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=Model),
    )

    dimension = prepare_semantic_model("local-model", local_files_only=True)

    assert dimension == 2
    assert observed["model_id"] == "local-model"
    assert observed["local_files_only"] is True
    assert observed["texts"] == ["PaperPilot semantic retrieval readiness check"]


@pytest.mark.parametrize("vector", [[], [float("nan")], [0.0, 0.0], [True]])
def test_prepare_semantic_model_rejects_invalid_vectors(
    monkeypatch: pytest.MonkeyPatch,
    vector: list[object],
) -> None:
    class Model:
        def __init__(self, model_id: str, *, local_files_only: bool) -> None:
            pass

        def encode(self, texts, **kwargs):
            return [vector]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=Model),
    )

    with pytest.raises(ValueError):
        prepare_semantic_model("invalid-model", local_files_only=True)
