from __future__ import annotations

from evaluation import embedder as embedder_module


def test_embedder_cache_is_scoped_by_model_name(monkeypatch) -> None:
    created: list[tuple[str, object]] = []

    def fake_sentence_transformer(
        model_name: str,
        *,
        local_files_only: bool,
    ) -> object:
        instance = object()
        created.append((f"{model_name}:{local_files_only}", instance))
        return instance

    monkeypatch.setattr(
        embedder_module,
        "SentenceTransformer",
        fake_sentence_transformer,
    )
    monkeypatch.setattr(embedder_module.Embedder, "_model_instances", {})

    english = embedder_module.Embedder("english-model")._load_model()
    multilingual = embedder_module.Embedder(
        "multilingual-model",
        local_files_only=True,
    )._load_model()
    english_again = embedder_module.Embedder("english-model")._load_model()

    assert english is english_again
    assert multilingual is not english
    assert [name for name, _ in created] == [
        "english-model:False",
        "multilingual-model:True",
    ]
