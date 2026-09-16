"""Placeholder suite — proves the package imports and the layout is intact.

Replaced with real tests as each step is built.
"""

from pathlib import Path

import bench_outreach

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_imports():
    assert bench_outreach is not None


def test_expected_layout_exists():
    expected = [
        "src/bench_outreach/common",
        "src/bench_outreach/loader",
        "src/bench_outreach/gateway",
        "src/bench_outreach/email_agent",
        "src/bench_outreach/event_receiver",
        "src/bench_outreach/research_agent",
        "docs/DESIGN_SOURCE.md",
        "docs/OPEN_QUESTIONS.md",
        "docs/STEPS.md",
        "CLAUDE.md",
    ]
    missing = [p for p in expected if not (REPO_ROOT / p).exists()]
    assert not missing, f"missing from the repo: {missing}"


def test_no_real_data_committed():
    """data/ holds synthetic samples only — never a real consultant list."""
    data = REPO_ROOT / "data"
    stray = [p.name for p in data.glob("*") if p.is_file()]
    assert not stray, f"loose data files at data/ root: {stray}"
