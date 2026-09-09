"""Original suites expose run() or __main__, not pytest assertions."""

import subprocess
import sys
from pathlib import Path
import pytest


@pytest.mark.parametrize(
    "name", ["sectors", "alerts", "dedupe", "location", "simplify", "trackers"]
)
def test_legacy_suite(name):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / f"test_{name}.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
