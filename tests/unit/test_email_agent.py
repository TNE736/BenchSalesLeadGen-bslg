"""One lead end to end, with fake HubSpot, a scripted model and a fake sender.

The model is a function that receives the three tools and calls them in a fixed
order — the same calls Claude makes, minus the network. Every safety rule lives in
the tools or in the steps around them, so this is where those rules are proved.
"""

import itertools
import json as _json
from pathlib import Path

import pytest

from bench_outreach.common.hubspot_mcp import HubSpotMCPError
from bench_outreach.common.trace import Trace
from bench_outreach.email_agent import agent as transcript
from bench_outreach.email_agent.agent import (EmailAgent, ModelError, check_sender_identity,
                                              load_config)
from bench_outreach.email_agent.sender import DryRunSender, SendError, SendResult

REPO_ROOT = Path(__file__).resolve().parents[2]

CONTACT = {"firstname": "Srinivas", "lastname": "Chittimalla",
           "email": "srinivas@example.com", "technology": "Salesforce",
           "title": "Salesforce Lead Developer", "decision_maker": "true"}

SKILL, ASSET, REFERENCE = ("outreach/SKILL.md", "outreach/assets/salesforce.json",
                           "outreach/references/salesforce.md")


class FakeHubSpot:
    """Stands in for the MCP client: records every call, answers from a dict."""

    server_url = "https://mcp.hubspot.test"

    def __init__(self, contact: dict | None = None, raise_on_read: Exception | None = None):
        self.contact = dict(contact) if contact is not None else dict(CONTACT)
        self.raise_on_read = raise_on_read
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool == "get_crm_objects":
            ids = args["objectIds"]
            assert all(isinstance(i, int) for i in ids), \
                f"objectIds must be numbers, got {ids!r} — HubSpot rejects strings"
            if self.raise_on_read:
                raise self.raise_on_read
            return {"objects": [{"id": str(ids[0]), "properties": self.contact}]}
        if tool == "manage_crm_objects":
            self.contact.update(args["updateRequest"]["objects"][0]["properties"])
            return {"updateResults": {"summary": {"updated": 1, "failed": 0}}}
        raise AssertionError(f"unexpected tool {tool}")

    def statuses_written(self) -> list[str]:
        return [a["updateRequest"]["objects"][0]["properties"]["email_status"]
                for t, a in self.calls if t == "manage_crm_objects"]


class RecordingSender:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.sent: list[dict] = []

    def send(self, to, subject, body, variables, html=""):
        if self.fail:
            raise self.fail
        self.sent.append({"to": to, "subject": subject, "body": body, "html": html, **variables})
        return SendResult(ok=True, message_id="<mg-1@example>", detail="accepted")


def scripted(*steps):
    """A fake model: the tool calls it makes, in order. Returns each tool's reply."""
    def model(system, user, tools):
        model.replies = [getattr(tools, name)(**kw) for name, kw in steps]
    model.replies = []
    return model


READ_ALL = [("read_skill_file", {"path": SKILL}), ("read_skill_file", {"path": ASSET}),
            ("read_skill_file", {"path": REFERENCE})]
SEND = ("send_email", {"subject": "Salesforce role", "body": "Hi Srinivas,\n\nBest,\nPriya"})
HAPPY = scripted(*READ_ALL, SEND)

_TRACE_N = itertools.count()


def _trace(tmp_path) -> tuple[Trace, Path]:
    """A real Trace writing into tmp_path (unique service name: Trace caches one
    file handler per service)."""
    service = f"test{next(_TRACE_N)}"
    return Trace(service, "run-1", "test run", tmp_path), tmp_path / f"{service}.jsonl"


def _events(path: Path, name: str) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = _json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == name:
            out.append(rec)
    return out


def build(hs, sender=None, contact_overrides=None, model=None, dry_run=False):
    config = load_config(REPO_ROOT / "config" / "email.yaml")
    config["sender"] = {"name": "Priya Nair", "company": "Tek Ninjas",
                        "from_email": "p@mg.example.com",
                        "postal_address": "1 Example Way, Charlotte NC"}
    # Absolute: conftest points agent.REPO_ROOT at tmp_path so transcripts stay out
    # of the repo, which would otherwise send the skill lookup there too.
    config["skill"]["path"] = str(REPO_ROOT / "skills")
    config["skill"]["system_prompt"] = str(REPO_ROOT / "config" / "EMAIL_AGENT.md")
    if contact_overrides:
        hs.contact.update(contact_overrides)
    return EmailAgent(sender=sender or RecordingSender(), config=config, hubspot=hs,
                      dry_run=dry_run, model_fn=model or HAPPY)


# ------------------------------------------------------------------ happy path
def test_sends_once_and_records_sent():
    hs, sender = FakeHubSpot(), RecordingSender()
    outcome = build(hs, sender).work("551358867185", trigger_id="trg-1")
    assert outcome.status == "sent" and outcome.subject == "Salesforce role"
    assert sender.sent[0]["to"] == "srinivas@example.com"
    assert hs.statuses_written() == ["SENT"]
    assert outcome.reference == f"{SKILL},{ASSET},{REFERENCE}", "the files the model read"


def test_footer_and_correlation_ids_travel_with_the_message():
    sender = RecordingSender()
    build(FakeHubSpot(), sender).work("777", trigger_id="trg-9")
    msg = sender.sent[0]
    assert "1 Example Way, Charlotte NC" in msg["body"] and "Unsubscribe:" in msg["body"]
    assert msg["bo_object_id"] == "777" and msg["bo_trigger_id"] == "trg-9"
    assert REFERENCE in msg["bo_reference"]
    assert msg["bo_technology"] == "Salesforce" and msg["bo_title"] == "Salesforce Lead Developer"


def test_the_model_is_told_which_skills_exist_and_given_the_record_as_data():
    seen = {}

    def model(system, user, tools):
        seen.update(system=system, user=user)
        HAPPY(system, user, tools)
    build(FakeHubSpot(), model=model).work("1")
    assert "outreach" in seen["system"] and "Write one short outreach email" in seen["system"]
    assert "Sign as: Priya Nair" in seen["system"]
    assert "copied from our CRM" in seen["user"] and "Salesforce Lead Developer" in seen["user"]
    assert "srinivas@example.com" not in seen["user"], "the model has no use for the address"
    assert "email_status" not in seen["user"]


# ---------------------------------------------------------------------- guards
def test_missing_email_is_failed_with_a_reason():
    hs = FakeHubSpot()
    outcome = build(hs, contact_overrides={"email": ""}).work("1")
    assert outcome.status == "failed" and "no email address" in outcome.reason
    assert hs.statuses_written() == ["FAILED"]


@pytest.mark.parametrize("status", ["SENT", "DELIVERED", "OPENED", "UNSUBSCRIBED"])
def test_already_contacted_is_skipped_and_nothing_is_sent(status):
    hs, sender = FakeHubSpot(), RecordingSender()
    outcome = build(hs, sender, contact_overrides={"email_status": status}).work("1")
    assert outcome.status == "skipped" and not sender.sent
    assert hs.statuses_written() == []


def test_hubspot_optout_is_respected():
    sender = RecordingSender()
    outcome = build(FakeHubSpot(), sender, contact_overrides={"hs_email_optout": "true"}).work("1")
    assert outcome.status == "skipped" and "opted out" in outcome.reason and not sender.sent


def test_decision_maker_rechecked_at_send_time():
    sender = RecordingSender()
    outcome = build(FakeHubSpot(), sender, contact_overrides={"decision_maker": "false"}).work("1")
    assert outcome.status == "skipped" and not sender.sent


# ------------------------------------------------------- the model's decisions
def test_a_lead_the_skill_cannot_write_to_is_flagged_not_emailed():
    """The model reads the skill, finds no entry for the title, and says so."""
    hs, sender = FakeHubSpot(), RecordingSender()
    model = scripted(("read_skill_file", {"path": SKILL}),
                     ("flag_lead", {"reason": "no assets entry for the title Golang Developer"}))
    outcome = build(hs, sender, contact_overrides={"technology": "Golang"}, model=model).work("1")
    assert outcome.status == "failed" and "Golang Developer" in outcome.reason
    assert not sender.sent
    assert hs.statuses_written() == ["SENT", "FAILED"]      # claim taken, then released


def test_claim_is_written_before_the_model_runs():
    """The ordering that stops a retry double-sending while the model is thinking."""
    seen: list[str] = []
    hs = FakeHubSpot()

    def model(system, user, tools):
        seen.extend(hs.statuses_written())
        HAPPY(system, user, tools)
    build(hs, model=model).work("1")
    assert seen == ["SENT"], "status must be claimed before the model call, not after"


def test_send_failure_writes_failed():
    hs = FakeHubSpot()
    outcome = build(hs, RecordingSender(fail=SendError("Mailgun refused"))).work("1")
    assert outcome.status == "failed" and "Mailgun refused" in outcome.reason
    assert hs.statuses_written() == ["SENT", "FAILED"]


def test_model_api_failure_writes_failed_and_sends_nothing():
    def model(system, user, tools):
        raise ModelError("model unreachable")
    hs, sender = FakeHubSpot(), RecordingSender()
    outcome = build(hs, sender, model=model).work("1")
    assert outcome.status == "failed" and "compose" in outcome.reason and not sender.sent
    assert hs.statuses_written() == ["SENT", "FAILED"]


def test_a_model_that_stops_without_acting_is_a_failed_lead_not_a_silent_one():
    hs, sender = FakeHubSpot(), RecordingSender()
    outcome = build(hs, sender, model=scripted(*READ_ALL)).work("1")
    assert outcome.status == "failed" and "without sending" in outcome.reason
    assert hs.statuses_written() == ["SENT", "FAILED"]


def test_hubspot_read_failure_propagates_so_the_gateway_retries():
    agent = build(FakeHubSpot(raise_on_read=HubSpotMCPError("HubSpot down")))
    with pytest.raises(HubSpotMCPError):
        agent.work("1")


# ------------------------------------------------ rules enforced inside the tools
def test_send_is_refused_until_the_skill_and_reference_were_read():
    """The evidence rule, enforced in code: no email leaves without SKILL.md and the
    technology reference having been read this run."""
    sender = RecordingSender()
    model = scripted(SEND,                                     # nothing read yet
                     ("read_skill_file", {"path": SKILL}), SEND,   # no reference yet
                     ("read_skill_file", {"path": REFERENCE}), SEND)
    outcome = build(FakeHubSpot(), sender, model=model).work("1")
    assert "read the skill's SKILL.md" in model.replies[0]
    assert "technology reference" in model.replies[2]
    assert model.replies[4].startswith("Sent")
    assert outcome.status == "sent" and len(sender.sent) == 1


def test_a_second_send_is_refused():
    sender = RecordingSender()
    model = scripted(*READ_ALL, SEND, SEND)
    build(FakeHubSpot(), sender, model=model).work("1")
    assert len(sender.sent) == 1
    assert "already been sent" in model.replies[-1]


def test_a_flag_after_a_send_is_refused():
    model = scripted(*READ_ALL, SEND, ("flag_lead", {"reason": "changed my mind"}))
    outcome = build(FakeHubSpot(), model=model).work("1")
    assert outcome.status == "sent"
    assert "already sent" in model.replies[-1]


def test_a_path_outside_the_skills_folder_is_refused():
    model = scripted(("read_skill_file", {"path": "../config/email.yaml"}),
                     ("read_skill_file", {"path": "../../.env"}), *READ_ALL, SEND)
    build(FakeHubSpot(), model=model).work("1")
    assert model.replies[0].startswith("Error") and model.replies[1].startswith("Error")


# ---------------------------------------------------------------------- dry run
def test_dry_run_writes_a_draft_file_and_never_touches_hubspot(tmp_path):
    hs = FakeHubSpot()
    sender = DryRunSender(tmp_path / "drafts.md")
    outcome = build(hs, sender, dry_run=True).work("551358867185")
    assert outcome.status == "sent" and outcome.dry_run is True
    assert hs.statuses_written() == [], "dry run must not write to HubSpot"
    text = (tmp_path / "drafts.md").read_text()
    assert "srinivas@example.com" in text and "Salesforce role" in text


# ------------------------------------------------------------------- the logs
def test_mcp_read_is_logged_with_property_names_but_never_values(tmp_path):
    t, log_file = _trace(tmp_path)
    build(FakeHubSpot()).work("551358867185", t=t)
    hops = [e for e in _events(log_file, "outbound_call") if e["endpoint"] == "get_crm_objects"]
    assert len(hops) == 1 and hops[0]["ok"] is True
    assert hops[0]["params"]["objectIds"] == [551358867185]
    read = _events(log_file, "contact_read")[0]
    assert "technology" in read["properties_returned"], "property NAMES are logged"
    assert "srinivas@example.com" not in log_file.read_text(encoding="utf-8")


def test_every_skill_file_the_model_read_is_logged_with_its_version(tmp_path):
    """Not just that a skill was used — WHICH files, and which version of each."""
    t, log_file = _trace(tmp_path)
    build(FakeHubSpot()).work("1", t=t)
    reads = _events(log_file, "skill_file_read")
    assert [r["path"] for r in reads] == [SKILL, ASSET, REFERENCE]
    assert all(len(r["sha"]) == 12 for r in reads)
    built = _events(log_file, "prompt_built")[0]
    assert "outreach/SKILL.md@" in built["skills"][0].replace("\\", "/")
    assert len(built["prompt_sha"]) == 12
    done = _events(log_file, "model_done")[0]
    assert done["sent"] is True and done["files_read"] == [SKILL, ASSET, REFERENCE]


def test_dry_run_says_out_loud_that_it_wrote_nothing(tmp_path):
    t, log_file = _trace(tmp_path)
    build(FakeHubSpot(), dry_run=True).work("1", t=t)
    skipped = _events(log_file, "hubspot_write_skipped")
    assert skipped and skipped[0]["would_write"] == "SENT"


def test_live_write_is_logged(tmp_path):
    t, log_file = _trace(tmp_path)
    build(FakeHubSpot(), dry_run=False).work("1", t=t)
    writes = [e for e in _events(log_file, "outbound_call") if e["endpoint"] == "manage_crm_objects"]
    assert [w["params"]["email_status"] for w in writes] == ["SENT"]
    assert all(w["ok"] and w["stream"] == "audit" for w in writes)


def test_sender_identity_check_names_what_is_missing():
    assert set(check_sender_identity({"name": "x"})) == {"company", "from_email", "postal_address"}
    assert check_sender_identity({"name": "a", "company": "b", "from_email": "c",
                                  "postal_address": "d"}) == []


# ------------------------------------------------------- the test redirect
def test_redirect_diverts_the_email_and_names_the_real_recipient(monkeypatch):
    monkeypatch.setenv("EMAIL_AGENT_REDIRECT_TO", "stephen.m@tekninjas.com")
    hs, sender = FakeHubSpot(), RecordingSender()
    outcome = build(hs, sender).work("1")
    assert outcome.status == "sent"
    message = sender.sent[0]
    assert message["to"] == "stephen.m@tekninjas.com"
    assert message["bo_intended_to"] == "srinivas@example.com"
    assert message["bo_redirected"] == "true"
    assert message["subject"].startswith("[TEST -> srinivas@example.com] ")


def test_redirect_does_not_burn_the_lead(monkeypatch):
    monkeypatch.setenv("EMAIL_AGENT_REDIRECT_TO", "stephen.m@tekninjas.com")
    hs = FakeHubSpot()
    build(hs, RecordingSender()).work("1")
    assert hs.statuses_written() == []
    assert "email_status" not in hs.contact


def test_without_the_variable_nothing_changes(monkeypatch):
    monkeypatch.delenv("EMAIL_AGENT_REDIRECT_TO", raising=False)
    hs, sender = FakeHubSpot(), RecordingSender()
    build(hs, sender).work("1")
    message = sender.sent[0]
    assert message["to"] == "srinivas@example.com"
    assert "bo_intended_to" not in message
    assert not message["subject"].startswith("[TEST")
    assert hs.statuses_written() == ["SENT"]


def test_a_blank_variable_is_not_a_redirect(monkeypatch):
    monkeypatch.setenv("EMAIL_AGENT_REDIRECT_TO", "   ")
    hs, sender = FakeHubSpot(), RecordingSender()
    build(hs, sender).work("1")
    assert sender.sent[0]["to"] == "srinivas@example.com"
    assert hs.statuses_written() == ["SENT"]


def test_both_parts_are_sent():
    sender = RecordingSender()
    build(FakeHubSpot(), sender).work("1")
    message = sender.sent[0]
    assert "Unsubscribe: %unsubscribe_url%" in message["body"]
    assert '<a href="%unsubscribe_url%">Unsubscribe</a>' in message["html"]
    assert message["html"].startswith("<p>")


# ------------------------------------------------------------- transcripts
# The prompt, the tool calls and the sent email are kept as TEXT, not just as shas.
# These files carry personal data: words here, fingerprints in the JSONL.

@pytest.fixture
def _no_transcript_switch(monkeypatch):
    monkeypatch.delenv("BENCH_RUN_TRANSCRIPTS", raising=False)


def test_sections_accumulate_into_one_file_per_run(_no_transcript_switch):
    transcript.write_transcript("trg-1", "Prompt sent to the model", "You are writing...")
    transcript.write_transcript("trg-1", "Raw reply from the model", '{"subject": "x"}')
    rel = transcript.write_transcript("trg-1", "Email as sent", "Subject: x\n\nHello.")
    text = (transcript.RUNS / "trg-1.md").read_text(encoding="utf-8")
    assert text.count("## ") == 3
    assert "You are writing..." in text and "Hello." in text
    assert rel.endswith("runs/trg-1.md")
    assert text.index("Prompt") < text.index("Raw reply") < text.index("Email as sent")


def test_the_header_warns_what_is_in_the_file(_no_transcript_switch):
    transcript.write_transcript("trg-2", "Prompt sent to the model", "...")
    assert "personal data" in (transcript.RUNS / "trg-2.md").read_text(encoding="utf-8").lower()


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BENCH_RUN_TRANSCRIPTS", "false")
    assert transcript.write_transcript("trg-3", "Prompt", "secret") == ""
    assert not (transcript.RUNS / "trg-3.md").exists()


def test_an_unwritable_disk_never_stops_a_send(monkeypatch, _no_transcript_switch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(transcript.Path, "mkdir", boom)
    assert transcript.write_transcript("trg-4", "Prompt", "...") == ""


def test_a_run_id_cannot_escape_the_runs_folder(_no_transcript_switch):
    target = transcript.transcript_path("../../etc/passwd")
    assert target.parent == transcript.RUNS and "/" not in target.name
    transcript.write_transcript("../../etc/passwd", "Prompt", "...")
    assert target.exists()
