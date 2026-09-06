"""Offline preflight for the isolated ten-case multi-Agent batch."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
DRB = HERE.parent
sys.path.insert(0, str(DRB))
sys.path.insert(0, str(DRB / "paperpilot_snapshot"))

import freeze_protocol as freeze
import holdout_multi_runner as runner


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    runner.patch_output_root()
    spec = runner.runtime.verify_seal()
    audit = json.loads((HERE / "batch_adapter_audit.json").read_text(encoding="utf-8"))
    if sha(HERE / "holdout_multi_runner.py") != audit["adapter"]["sha256"]:
        raise ValueError("Batch runner hash changed")
    if tuple(spec["holdout_ids"]) != runner.ORDER:
        raise ValueError("Holdout order changed")
    if (HERE / "runs").exists():
        raise ValueError("Runs directory already exists before launch")
    dummy = HERE / "offline-path-check"
    config = runner.runtime.run_config("multi", dummy)
    differences = freeze.differences(freeze.read(HERE / "multi.config.json"), config)
    if set(differences) != set(spec["runtime_path_overrides"]):
        raise ValueError("Runtime adapter changes more than storage paths")
    limits = config["research"]["limits"]
    if limits["max_total_agents"] != 10 or limits["max_concurrent_agents"] != 10 or limits["max_fork_depth"] != 2:
        raise ValueError("Default multi-Agent limits changed")
    result = {
        "status": "passed",
        "network_calls": 0,
        "benchmark": audit["benchmark"],
        "paperpilot_source_commit": spec["source_commit"],
        "runner_sha256": audit["adapter"]["sha256"],
        "order": list(runner.ORDER),
        "arm": "multi",
        "runtime_path_differences": sorted(differences),
        "limits": limits,
        "tools": config["tools"]["enabled"],
        "research_model": spec["observed_environment"]["research_model"],
        "judge": spec["judge"],
    }
    runner.runtime.dump(HERE / "preflight.json", result)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
