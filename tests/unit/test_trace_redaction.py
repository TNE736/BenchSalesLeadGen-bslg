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
    written = (tmp_path / "redact_t.jsonl").read_text(encoding="utf-8")
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
