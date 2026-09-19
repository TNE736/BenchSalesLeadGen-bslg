"""Every script in scripts/ must still import against the current code.

Written after a refactor removed two arguments from `Skill.load()` and the suite
stayed green — because nothing tested `scripts/`. The operator found it by running
`dry_run.py` and getting a TypeError. Anything the operator runs by hand is part of
the product; this is the cheapest possible guard on it.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[2] / "scripts").glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_imports(script: Path):
    spec = importlib.util.spec_from_file_location(f"_script_{script.stem}", script)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        pass                      # argparse or an early return; the import worked


def test_there_are_scripts_to_check():
    """Guards the guard: a glob that matches nothing would pass silently."""
    assert len(SCRIPTS) >= 4
