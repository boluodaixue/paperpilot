"""Check or download the local sentence-transformers model used by PaperPilot."""
from __future__ import annotations

import argparse
import math
import sys
from numbers import Real
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.research.runtime import load_config


def _runtime_model(config: dict[str, Any]) -> tuple[str, bool]:
    runtime = config.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("runtime configuration must be a mapping")
    model_id = runtime.get(
        "semantic_embedding_model",
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    enabled = runtime.get("semantic_retrieval_enabled", False)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("runtime.semantic_embedding_model must be a non-empty string")
    if not isinstance(enabled, bool):
        raise ValueError("runtime.semantic_retrieval_enabled must be a boolean")
    return model_id.strip(), enabled


def prepare_semantic_model(
    model_id: str,
    *,
    local_files_only: bool,
) -> int:
    """Load one model and validate a bounded embedding result."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_id, local_files_only=local_files_only)
    vectors: Sequence[Sequence[object]] = model.encode(
        ["PaperPilot semantic retrieval readiness check"],
        normalize_embeddings=True,
        show_progress_bar=not local_files_only,
    )
    if len(vectors) != 1 or len(vectors[0]) == 0:
        raise ValueError("embedding model returned an empty readiness vector")
    values: list[float] = []
    for raw in vectors[0]:
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise ValueError("embedding model returned a non-numeric value")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("embedding model returned a non-finite value")
        values.append(value)
    if math.sqrt(sum(value * value for value in values)) <= 0:
        raise ValueError("embedding model returned a zero vector")
    return len(values)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the configured local semantic model or download it once."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Only use already cached model files (default).",
    )
    mode.add_argument(
        "--download",
        action="store_true",
        help="Allow sentence-transformers to download the configured model.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional YAML config path; defaults to configs/default.yaml.",
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    model_id, enabled = _runtime_model(config)
    local_files_only = not args.download
    action = "checking local cache" if local_files_only else "downloading if needed"
    print(f"Semantic model: {model_id} ({action})")
    try:
        dimension = prepare_semantic_model(
            model_id,
            local_files_only=local_files_only,
        )
    except Exception as exc:
        print(
            f"Semantic model is not ready: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        if local_files_only:
            print(
                "Run `python scripts/prepare_semantic_model.py --download` once, "
                "then repeat --check.",
                file=sys.stderr,
            )
        print("PaperPilot will continue safely with SQLite FTS5.", file=sys.stderr)
        return 1
    print(f"Semantic model is ready (dimension={dimension}).")
    if enabled:
        print("Configured retrieval mode: hybrid (FTS5 + semantic + WikiLink).")
    else:
        print(
            "The model is ready, but semantic retrieval is disabled. Set "
            "runtime.semantic_retrieval_enabled to true when you want hybrid retrieval."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
