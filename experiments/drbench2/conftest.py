"""Keep local-only frozen integration fixtures out of repository-wide CI.

The archived adapter tests remain runnable after ``prepare.py`` creates the
ignored source snapshot and benchmark inputs.  A fresh Git checkout does not
contain those local artifacts, so collecting the tests there would fail during
class setup rather than test committed code.
"""

from pathlib import Path


HERE = Path(__file__).resolve().parent
FROZEN_FIXTURES_AVAILABLE = all((
    (HERE / "paperpilot_snapshot" / "configs" / "default.yaml").is_file(),
    (HERE / "research_inputs").is_dir(),
    (HERE / "judge_inputs").is_dir(),
))

collect_ignore: list[str] = []
if not FROZEN_FIXTURES_AVAILABLE:
    collect_ignore.extend((
        "test_freeze_protocol.py",
        "test_continuation_adapter.py",
    ))
