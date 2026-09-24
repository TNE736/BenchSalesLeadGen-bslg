"""§4 -- a run or step that iterated items ends with the count by status and
the keys of the ones that did not succeed.

The point of `failed_keys` is that nobody has to scroll back through forty-seven
`item_done` records to find the one that broke. A regression here is silent: the
run still succeeds, the counts just stop being there.
"""

import json

import pytest

from bench_outreach.common.logging_core import Logger, Settings


@pytest.fixture
def logger(tmp_path):
    #: Built directly, not through `Settings.read`: conftest sets LOG_DIR for
    #: the whole suite, and §1 has the environment beat the argument.
    return Logger(Settings(service="probe", log_dir=tmp_path, mode="normal"))


def records(logger, event):
    lines = (logger.settings.log_dir / "process.log").read_text(encoding="utf-8").splitlines()
    return [r for r in (json.loads(x) for x in lines if x.strip()) if r["event"] == event]


def test_a_step_that_iterated_ends_with_counts_and_the_keys_that_failed(logger):
    with logger.run("cli"):
        with logger.step("compose") as frame:
            for number, (key, how) in enumerate(
                    [("540690769648", "ok"), ("540690771102", "skipped"),
                     ("540690773311", "failed")], start=1):
                with logger.item(number, 3, object_id=key) as item:
                    if how == "skipped":
                        item.skipped("already contacted")
                    elif how == "failed":
                        item.failed("MailgunError: 550 rejected")
            frame.ok(composed=1)

    outputs = records(logger, "step_out")[0]["outputs"]
    assert outputs["composed"] == 1                      # the step's own output survives
    assert (outputs["total"], outputs["ok"]) == (3, 1)
    assert (outputs["failed"], outputs["skipped"]) == (1, 1)
    assert outputs["failed_keys"] == ["540690773311"]
    assert outputs["skipped_keys"] == ["540690771102"]


def test_the_run_carries_the_same_tally_as_the_step(logger):
    with logger.run("cli"):
        with logger.step("compose"):
            with logger.item(1, 1, object_id="540690773311") as item:
                item.failed("MailgunError: 550 rejected")

    assert records(logger, "run_end")[0]["outputs"]["failed_keys"] == ["540690773311"]


def test_a_step_that_iterated_nothing_carries_no_tally(logger):
    """A step with no items has nothing to count, and says nothing."""
    with logger.run("cli"):
        with logger.step("read_leads") as frame:
            frame.ok(leads=3)

    assert records(logger, "step_out")[0]["outputs"] == {"leads": 3}


def test_the_number_is_the_key_when_the_caller_passed_none(logger):
    with logger.run("cli"):
        with logger.step("compose"):
            with logger.item(7, 9) as item:
                item.failed("no object_id to name it by")

    assert records(logger, "step_out")[0]["outputs"]["failed_keys"] == ["7"]
