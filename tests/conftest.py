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
