"""Shared test fixtures."""

import os
import tempfile

import pytest

#: Before ANY application module is imported. Both apps run `create_app()` at
#: import, which writes a `service_start` with a fresh trace id -- into the real
#: `logs/agents/` if nothing says otherwise. Thirty test runs put seventy-nine
#: phantom "runs" in the operator's log. Part 1's environment variable is the
#: right lever: set here, at conftest import, it is in force when the test
#: modules import the apps.
os.environ.setdefault("LOG_DIR",
                      tempfile.mkdtemp(prefix="bench-outreach-test-records-"))

from bench_outreach.email_agent import agent as transcript


@pytest.fixture(autouse=True)
def _redirect_is_opt_in(monkeypatch):
    """No test inherits the developer's EMAIL_AGENT_REDIRECT_TO.

    Turning the redirect on in .env used to change the behaviour of five unrelated
    tests, which makes the suite a report on one machine's config rather than on the
    code. A test that wants the redirect sets it itself.
    """
    monkeypatch.delenv("EMAIL_AGENT_REDIRECT_TO", raising=False)


@pytest.fixture(autouse=True)
def _transcripts_stay_in_tmp(tmp_path, monkeypatch):
    """No test writes into the repo's real logs/ folder.

    compose() writes a run transcript as a side effect, so every test that composed
    an email was quietly dropping a file into logs/email_agent/runs/ — named after
    whatever run id the test happened to use. Harmless but wrong: a test suite that
    leaves litter in the working tree is a suite you stop trusting.
    """
    monkeypatch.setattr(transcript, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(transcript, "REPO_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _records_stay_in_tmp(tmp_path, monkeypatch):
    """No test writes into the repo's real logs/.

    LOG_DIR is the spec's own setting (§1) and outranks everything, so it is
    the right lever. Both services' loggers are reset around each test, since
    each holds one for the life of the process.
    """
    from bench_outreach.common import settings
    from bench_outreach.gateway import app as gateway_app, gateway_logging as glog
    from bench_outreach.email_agent import email_agent_logging as elog
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gateway_app, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    glog.reset()
    elog.reset()
    yield
    glog.reset()
    elog.reset()


@pytest.fixture(autouse=True)
def _inside_a_run():
    """Most tests call something that logs. A run gives those records a trace
    id the way a real caller does; a test about where the id is born opens its
    own."""
    from bench_outreach.common import logging_core as core
    token = core._TRACE.set(core.new_trace_id())
    try:
        yield
    finally:
        core._TRACE.reset(token)
