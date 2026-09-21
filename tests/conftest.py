"""Shared test fixtures."""

import pytest

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
def _jsonl_stays_in_tmp(tmp_path, monkeypatch):
    """No test writes into the repo's real logs/*.jsonl either.

    The transcript fixture above covered the .md files and stopped there, so the
    suite was still appending to logs/gateway/process.jsonl -- one pytest run left
    127 lines of fixture traffic (`route "dm"`, `contact 101`) in the file you read
    after a real run. `create_app(log_dir=...)` does not prevent it: `_logger`
    returns early when the logger already has a handler, so whichever log_dir was
    seen FIRST in the process wins and every later one is silently ignored.

    Pointing REPO_ROOT at tmp_path is what actually moves the files, because that
    is where the default `log_dir` is computed from.
    """
    from bench_outreach.common import settings, trace as trace_mod
    from bench_outreach.gateway import app as gateway_app
    # `trace._logger` does `from .settings import REPO_ROOT` at call time, so the
    # settings module is the one that has to move -- patching trace's own namespace
    # would do nothing.
    monkeypatch.setattr(settings, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(gateway_app, "REPO_ROOT", tmp_path)
    for logger in list(trace_mod.logging.Logger.manager.loggerDict):
        if logger.startswith("bench_outreach.trace."):
            live = trace_mod.logging.getLogger(logger)
            for handler in list(live.handlers):
                live.removeHandler(handler)
                handler.close()
