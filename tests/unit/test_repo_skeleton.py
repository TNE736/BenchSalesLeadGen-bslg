"""Layout guard — replaced piece by piece as steps are built."""

from pathlib import Path

import bench_outreach

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_imports():
    assert bench_outreach is not None


def test_expected_layout_exists():
    expected = [
        "src/bench_outreach/common",
        "src/bench_outreach/gateway",
        "config/gateway.yaml",
        "docs/DESIGN_SOURCE.md",
        "docs/OPEN_QUESTIONS.md",
        "docs/STEPS.md",
        "CLAUDE.md",
    ]
    missing = [p for p in expected if not (REPO_ROOT / p).exists()]
    assert not missing, f"missing from the repo: {missing}"

