"""redact() runs inside the emit path, so nothing written can go round it.

The design question these tests pin down: credentials are caught by the field
NAME, personal data by the field VALUE. That split is what lets
`properties_returned: ["email", "mobilephone"]` survive — a list of HubSpot
property names is the line that proves what we read, and a redactor keyed on the
word "email" would blank it and leave a line proving nothing.
"""

import json

from bench_outreach.common.trace import REDACTED, Trace, redact


def test_property_names_survive():
    """The trap. LQABR's research agent fell into it and left a scar in a comment."""
    out = redact({"properties_returned": ["email", "firstname", "mobilephone"]})
    assert out["properties_returned"] == ["email", "firstname", "mobilephone"]


def test_an_actual_address_does_not_survive():
    out = redact({"to": "bhumika.tewari13@gmail.com",
                  "detail": "sent to v@example.com just now"})
    assert out["to"] == REDACTED
    assert "v@example.com" not in out["detail"]


def test_a_phone_number_is_blanked_but_an_object_id_is_not():
    """Both are long digit runs. The id is logged deliberately and is what a
    person searches by; the phone is somebody's number."""
    out = redact({"object_id": "553374486245",
                  "mobilephone": "+1 (469) 731-0001",
                  "detail": "contact 553374486245 on 469-731-0001"})
    assert out["object_id"] == "553374486245"
    assert out["mobilephone"] == REDACTED
    assert "553374486245" in out["detail"], "the id must stay readable"
    assert "469-731-0001" not in out["detail"]


def test_credentials_go_by_field_name():
    out = redact({"api_key": "sk-ant-real", "signing_key": "abc",
                  "params": {"authorization": "Bearer xyz"}})
    assert out["api_key"] == REDACTED and out["signing_key"] == REDACTED
    assert out["params"]["authorization"] == REDACTED, "nested too"


def test_the_things_a_log_exists_to_show_are_kept():
    """A token COUNT is a number, not a token. A sha NAMES a file version."""
    out = redact({"input_tokens": 3392, "max_tokens": 1024,
                  "skill_sha": "78e5b7e538db",
                  "skill_file": "skills/outreach/SKILL.md"})
    assert out == {"input_tokens": 3392, "max_tokens": 1024,
                   "skill_sha": "78e5b7e538db",
                   "skill_file": "skills/outreach/SKILL.md"}


def test_redaction_cannot_be_bypassed_by_calling_the_trace_directly(tmp_path):
    """The guarantee: it runs inside _record, not at the call sites."""
    t = Trace("redact_t", "run", "t", tmp_path)
    t.event("careless", email="someone@example.com", api_key="sk-real")
    # Records land in per-stream files now; the guarantee must hold across all of them.
    written = "\n".join(f.read_text(encoding="utf-8") for f in sorted(tmp_path.glob("*.jsonl")))
    assert "someone@example.com" not in written
    assert "sk-real" not in written
    assert json.loads(written.splitlines()[-1])["api_key"] == REDACTED


# --------------------------------------------------------------------------
# Both cases below were found by reading a REAL run's log, not by reasoning
# about the code. Over-redaction is quiet: it protects nobody and destroys the
# fields the log exists to carry.
# --------------------------------------------------------------------------

MESSAGE_ID = "<20260919093735.358fb3dc6e278b9d@reply.tekninjas.com>"


def test_the_mailgun_message_id_survives():
    """It ends in a domain, so the email pattern eats it — and it is the only key
    that joins a delivered / opened / bounced webhook back to the run that sent
    the mail. Redacting it protects nobody and breaks delivery tracking."""
    out = redact({"message_id": MESSAGE_ID, "status": 200})
    assert out["message_id"] == MESSAGE_ID


def test_identifiers_are_matched_exactly_not_by_suffix():
    """`message_id` passes. A field that merely ends in _id does not, or anyone
    could widen the hole by choosing a variable name."""
    out = redact({"sender_id": "someone@example.com"})
    assert out["sender_id"] == REDACTED


def test_a_date_is_not_a_phone_number():
    """`drafts-2026-09-18.md` became `drafts-<redacted>.md`: eight digits and a
    hyphen was enough to read as a phone number."""
    out = redact({"reason": "dry-run -> drafts-2026-09-18.md",
                  "when": "2026-09-19", "us_style": "09/18/2026"})
    assert out["reason"] == "dry-run -> drafts-2026-09-18.md"
    assert out["when"] == "2026-09-19"
    assert out["us_style"] == "09/18/2026"


def test_real_phone_numbers_still_go():
    """The date guard must not become a hole. These are the shapes HubSpot holds."""
    for number in ("+1 (469) 731-0001", "469-731-0001", "469.731.0001",
                   "+91 98765 43210"):
        out = redact({"detail": f"called {number} today"})
        assert number not in out["detail"], f"{number} leaked"
        assert REDACTED in out["detail"]


def test_a_lead_address_in_a_subject_is_still_redacted():
    """The redirect prefixes the lead's address into the subject. `subject` is
    free text, not an identifier, so it must still be scrubbed."""
    out = redact({"message_id": MESSAGE_ID,
                  "params": {"subject": "[TEST -> lead@gmail.com] Architect requirement"}})
    assert out["message_id"] == MESSAGE_ID, "the id survives"
    assert "lead@gmail.com" not in out["params"]["subject"], "the address does not"


def test_audit_events_reach_the_console_not_just_the_file(tmp_path, capsys):
    """The run header promises AUDIT lines are marked. Until this was added only
    outbound_call kept that promise — the decisions went to the file and nowhere
    else, so the terminal showed the stream's cost half and hid its audit half."""
    t = Trace("audit_console", "trace-1", "t", tmp_path)
    t.event("guards_passed", stream="audit", decision_maker="true")
    t.event("contact_read", properties_returned=["email"])      # process: silent
    out = capsys.readouterr().out
    assert "AUDIT   guards_passed" in out
    assert "decision_maker=true" in out
    assert "contact_read" not in out, "a process event still has no console line"


def test_the_console_summary_is_redacted_too(tmp_path, capsys):
    """A terminal is as easy to paste into a ticket as a log file is."""
    t = Trace("audit_console2", "trace-2", "t", tmp_path)
    t.event("guard_stopped", stream="audit", value="lead@example.com",
            api_key="sk-ant-real")
    out = capsys.readouterr().out
    assert "lead@example.com" not in out and "sk-ant-real" not in out


def test_a_source_ip_survives():
    """`216.157.40.54` came out as <redacted> on a signature_verified record: twelve
    digits and dots read as a phone number. The source IP is the one field a security
    audit line exists to carry — without it the line proves nothing."""
    out = redact({"source_ip": "216.157.40.54", "note": "from 216.157.40.54"})
    assert out["source_ip"] == "216.157.40.54"
    assert out["note"] == "from 216.157.40.54"


def test_a_trace_id_survives_inside_free_text():
    """`trigger id bslg-20260921T113233-5c066ab10e87 minted` was logged as
    `bslg-20260921T<redacted>c066ab10e87`: the slice `113233-5` is seven digits and a
    hyphen. Losing the id inside the sentence that announces it defeats the log."""
    tid = "bslg-20260921T113233-5c066ab10e87"
    out = redact({"detail": f"trigger id {tid} minted for this lead", "trace_id": tid})
    assert out["detail"] == f"trigger id {tid} minted for this lead"
    assert out["trace_id"] == tid


def test_exempting_trace_ids_did_not_open_a_hole_next_to_one():
    """The exemption covers the id, not the rest of the sentence."""
    out = redact({"detail": "bslg-20260921T113233-5c066ab10e87 called +1 (469) 731-0001"})
    assert "bslg-20260921T113233-5c066ab10e87" in out["detail"]
    assert "469" not in out["detail"] and REDACTED in out["detail"]
