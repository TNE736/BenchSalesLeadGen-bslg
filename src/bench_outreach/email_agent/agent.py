"""The Email Agent: one lead, start to finish.

    read → guards → claim → the model works the lead with tools → outcome

Steps 1–3 run in code. Then Claude is handed the lead record, a catalogue of
skills (name + description only) and three tools: read_skill_file, send_email,
flag_lead. It picks the skill, reads only the files the skill points it to, and
calls send_email once. The guards, the claim, the footer and the one-send rule
are enforced in code inside the tools — never by the model.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from ..common.hubspot_mcp import HubSpotMCP, HubSpotMCPError
from ..common.settings import REPO_ROOT, env
from ..common.trace import AUDIT, Trace
from .sender import SendError, Sender, SendResult, UNSUBSCRIBE_TOKEN

SENT, FAILED = "SENT", "FAILED"
#: Run transcripts are ARTIFACTS, not logs: human-readable products of a run that
#: carry the lead's name and address. logs/ holds three machine-readable, PII-free
#: streams and nothing else, so these live apart — and so "can I delete this?" has
#: an answer.
ARTIFACTS = REPO_ROOT / "artifacts" / "email_agent"
RUNS = ARTIFACTS / "runs"
REQUIRED_SENDER_FIELDS = ("name", "company", "from_email", "postal_address")   # before a real send
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NOT_FOR_THE_MODEL = ("email", "mobilephone", "email_status", "hs_email_optout")  # PII + internal


# ------------------------------------------------------------------ config
def load_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for section in ("model", "skill", "sender", "guards"):
        if section not in config:
            raise ValueError(f"{path.name} is missing the '{section}' section")
    return config


def check_sender_identity(sender: dict[str, str]) -> list[str]:
    """Required sender fields still blank. Empty list means good to send."""
    return [f for f in REQUIRED_SENDER_FIELDS if not str(sender.get(f, "")).strip()]


def redirect_target() -> str:
    """EMAIL_AGENT_REDIRECT_TO: while set, every email lands here instead of with the
    lead, and the lead is not claimed. Read per send, so clearing it needs no restart."""
    return env("EMAIL_AGENT_REDIRECT_TO")


# ------------------------------------------------------------------- types
@dataclass
class Outcome:
    object_id: str
    status: str                 # sent | skipped | failed
    reason: str = ""
    subject: str = ""
    message_id: str | None = None
    reference: str = ""         # the skill files the model read, comma-separated
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Skipped(Exception):
    """A guard stopped this lead. Not an error — the guards doing their job."""


class Failed(Exception):
    """This lead could not be emailed. Written FAILED in HubSpot, never dropped."""


class ModelError(RuntimeError):
    """The model API failed. The lead is flagged, not sent."""


# ----------------------------------------------------------------- helpers
def _timed(t: Trace, service: str, endpoint: str, params: dict[str, Any],
           call: Callable[[], Any], usage: Callable[[Any], dict[str, Any]] | None = None) -> Any:
    """Run one call that leaves this process and write its audit line, pass or fail."""
    started = time.perf_counter()
    try:
        result = call()
    except Exception as exc:
        t.outbound_call(service=service, endpoint=endpoint, ok=False, error=str(exc),
                        params=params, duration_ms=round((time.perf_counter() - started) * 1000, 1))
        raise
    t.outbound_call(service=service, endpoint=endpoint, status=200, params=params,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    **(usage(result) if usage else {}))
    return result


def digest(text: str) -> str:
    """First 12 hex of sha256 — pins the exact version of a file or prompt in a log line."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _rel(path: Path) -> str:
    """Repo-relative path for a log line, so it names a file someone can open."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def transcript_path(trace_id: str) -> Path:
    return RUNS / f"{(trace_id or 'run').replace('/', '_')}.md"


def write_transcript(trace_id: str, heading: str, text: str) -> str:
    """Append one titled section to artifacts/email_agent/runs/<trace_id>.md.

    The JSONL carries only shas and this path; the words live here. Contains
    personal data: local only, never committed. Never raises.
    """
    if (env("BENCH_RUN_TRANSCRIPTS") or "true").lower() == "false":
        return ""
    target = transcript_path(trace_id)
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        new = not target.exists()
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            if new:
                fh.write(f"# run {trace_id}\n\n"
                         f"Started {datetime.now().isoformat(timespec='seconds')}.\n"
                         "Contains personal data — local only, never committed.\n")
            fh.write(f"\n\n## {heading}\n\n{text.rstrip()}\n")
    except OSError:
        return ""
    return target.relative_to(REPO_ROOT).as_posix()


# ------------------------------------------------------------------ skills
@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path          # the SKILL.md
    sha: str


def skill_catalogue(root: Path) -> list[SkillInfo]:
    """Level 1 of a skill: name + description from every <root>/*/SKILL.md frontmatter.

    Nothing else is read here. The body, assets and references are read by the
    model, on demand, through read_skill_file — that is what makes it a skill
    rather than a prompt template.
    """
    found = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        raw = skill_md.read_text(encoding="utf-8")
        match = FRONTMATTER.match(raw)
        meta = (yaml.safe_load(match.group(1)) if match else None) or {}
        description = str(meta.get("description") or "").strip()
        if not description:
            raise ValueError(f"{_rel(skill_md)} has no description in its frontmatter")
        found.append(SkillInfo(str(meta.get("name") or skill_md.parent.name), description,
                               skill_md, digest(raw)))
    if not found:
        raise ValueError(f"no skills under {root} (expected <name>/SKILL.md)")
    return found


#: How many `involves` phrases must appear word for word before an email may go out.
#: The floor, not the ceiling: SKILL.md asks the model for two to four, but only the
#: minimum is enforced here. Logged on every send as `exact`, so this number can be
#: raised later on evidence rather than on a guess.
GUARDRAIL_FLOOR = 2


def involves_for(root: Path, technology: str, title: str) -> tuple[list[str], str]:
    """The exact phrases this lead's role is allowed to be described with.

    Read by CODE, independently of whatever the model chose to read. That is the
    point of a guardrail: the check cannot trust the same source the model was free
    to paraphrase.

    Returns (phrases, asset path). An empty list means no asset covers this lead, so
    there is nothing to check — the guardrail stays silent rather than inventing a
    reason to block.
    """
    for asset in sorted(root.glob("*/assets/*.json")):
        try:
            data = json.loads(asset.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(data.get("technology", "")).strip().lower() != (technology or "").strip().lower():
            continue
        for name, entry in (data.get("titles") or {}).items():
            if name.strip().lower() == (title or "").strip().lower():
                return [str(x) for x in (entry.get("involves") or [])], _rel(asset)
    return [], ""


def _flat(text: str) -> str:
    """Lowercased, with every run of whitespace collapsed to one space.

    A phrase wrapped across two lines by the model is still the same phrase; without
    this, "data extension SQL\nsegmentation" would read as a rewrite when it is not.
    """
    return " ".join(text.lower().split())


def exact_phrases(body: str, phrases: list[str]) -> list[str]:
    """Which of `phrases` appear in `body` word for word."""
    flat = _flat(body)
    return [p for p in phrases if _flat(p) in flat]


def check_skill_files(root: Path) -> list[str]:
    """Startup sanity: every assets/*.json names a reference file that exists."""
    problems = []
    for asset in sorted(root.glob("*/assets/*.json")):
        try:
            ref = str(json.loads(asset.read_text(encoding="utf-8")).get("reference") or "")
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"{_rel(asset)}: {exc}")
            continue
        if ref and not (asset.parents[1] / "references" / ref).exists():
            problems.append(f"{_rel(asset)} names references/{ref}, which does not exist")
    return problems


@dataclass(frozen=True)
class SystemPrompt:
    """EMAIL_AGENT.md, read and checked once at startup.

    Three fields, because three are used: the body goes to the model, the path and
    the sha go in the log. The frontmatter's name and description are there for
    whoever opens the file, and stay there.
    """
    text: str           # the body, frontmatter stripped — what the model is sent
    path: Path
    sha: str            # of the RAW file, frontmatter included


def load_system_prompt(path: Path) -> SystemPrompt:
    """Read and validate EMAIL_AGENT.md. Once, at startup.

    Separate from render_system_prompt() because they happen at different times:
    this runs once when the service boots, that runs once per lead. Doing both per
    lead would re-read and re-parse the file for every contact, and a broken file
    would then surface on some unlucky lead at two in the morning instead of
    refusing to start. Same split skill_catalogue() uses.

    The prose used to be a string literal in this file, which made changing a rule
    the model must obey a code change and a redeploy. Parsed exactly like a
    SKILL.md — frontmatter then body — so there is one rule for both, not two.

    It is NOT an Agent Skill and is deliberately not under skills/. The spec
    (agentskills.io/specification) defines a skill as a directory holding a
    SKILL.md that an agent chooses to read. This is what the model is told BEFORE
    it chooses, and its rules have to hold whatever a skill says — so it cannot be
    one of the things a skill can override. Under skills/ it would also end up in
    skill_catalogue(), offering the agent its own system prompt to go and read.
    """
    if not path.exists():
        raise ValueError(f"no system prompt at {_rel(path)} — EMAIL_AGENT.md lives there")
    raw = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(raw)
    if not match:
        raise ValueError(f"{_rel(path)} has no YAML frontmatter")
    body = raw[match.end():].strip()
    if not body:
        raise ValueError(f"{_rel(path)} has nothing below its frontmatter")
    for token in ("{{SKILLS}}", "{{SENDER_NAME}}"):
        if token not in body:
            raise ValueError(f"{_rel(path)} is missing the {token} placeholder")
    return SystemPrompt(body, path, digest(raw))


def render_system_prompt(prompt: SystemPrompt, catalogue: list[SkillInfo],
                         identity: dict[str, str]) -> str:
    """Fill in the two placeholders. Once per lead.

    Plain replace, not str.format: the file is written by hand and will grow braces
    in JSON examples sooner or later, and `.format` would make every one of them an
    error to escape.
    """
    skills = "\n".join(f"- **{s.name}** — {s.description}  (read `{s.name}/SKILL.md`)"
                       for s in catalogue)
    return (prompt.text
            .replace("{{SKILLS}}", skills)
            .replace("{{SENDER_NAME}}", identity.get("name", "")))


def lead_message(contact: dict[str, Any]) -> str:
    """The lead's record, fenced and labelled as data. Address and phone are not
    sent: the model has no use for them and they are the fields that must not leak."""
    facts = {k: v for k, v in contact.items() if k not in NOT_FOR_THE_MODEL}
    return ("Write and send the outreach email for this lead. Their record, copied from our CRM:\n\n"
            f"```json\n{json.dumps(facts, indent=2, sort_keys=True, default=str)}\n```")


# ------------------------------------------------------------------- tools
class LeadTools:
    """The three tools the model gets for one lead, and the rules code enforces
    inside them. The model asks; this class decides."""

    def __init__(self, contact: dict[str, Any], object_id: str, trigger_id: str, sender: Sender,
                 identity: dict[str, str], skills_root: Path, divert: str, dry_run: bool, t: Trace):
        self.contact, self.object_id, self.trigger_id = contact, str(object_id), trigger_id
        self.sender, self.identity, self.root = sender, identity, skills_root.resolve()
        self.divert, self.dry_run, self.t = divert, dry_run, t
        self.files_read: list[str] = []
        self.sent: SendResult | None = None
        self.subject = ""
        self.flagged = ""
        self.send_error = ""
        self.guard_rejections = 0

    def read_skill_file(self, path: str) -> str:
        """`path` comes from the MODEL, so it is untrusted input to a filesystem read.

        The successful read is ordinary work and stays on the process stream. The
        REFUSAL is the security-relevant event — an attempt to read outside the
        skills folder — and it used to return an error string and record nothing.
        """
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            escaped = not target.is_relative_to(self.root)
            self.t.warn(f"refused skill file read: {path}"
                        + ("  (outside the skills folder)" if escaped else ""))
            self.t.event("skill_file_refused", stream=AUDIT, requested=path,
                         outside_skills_root=escaped)
            return f"Error: no such skill file: {path}"
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            return f"Error: could not read {path}: {exc}"
        rel = target.relative_to(self.root).as_posix()
        self.files_read.append(rel)
        self.t.ok(f"read {rel}  sha {digest(text)}")
        self.t.event("skill_file_read", path=rel, sha=digest(text), chars=len(text))
        return text

    def send_email(self, subject: str, body: str) -> str:
        if self.sent or self.send_error:
            return "Error: an email has already been sent for this lead. Do not send again — you are done."
        if not any(p.endswith("SKILL.md") for p in self.files_read):
            return "Error: read the skill's SKILL.md before sending."
        if not any("/references/" in p for p in self.files_read):
            return "Error: read the technology reference (references/<technology>.md) before sending."
        subject, body = subject.strip(), body.strip()
        if not subject or not body:
            return "Error: subject and body are both required."
        t = self.t

        rejected = self._guardrail(body)
        if rejected:
            return rejected

        t.step("Add the footer (postal address + unsubscribe) — by code, not the model")
        text = with_footer(body, self.identity, UNSUBSCRIBE_TOKEN)
        html_body = as_html(body, self.identity, UNSUBSCRIBE_TOKEN)
        sent_file = write_transcript(t.trace_id, "Email as sent", f"Subject: {subject}\n\n{text}")
        # Subject in full (our words); body as sha + length (it carries their name).
        t.event("draft", subject=subject, body_chars=len(body), body_sha=digest(body))
        t.event("footer_added", postal_address=self.identity["postal_address"],
                unsubscribe_token=UNSUBSCRIBE_TOKEN, final_chars=len(text),
                html_chars=len(html_body), sent_file=sent_file)

        email = (self.contact.get("email") or "").strip()
        recipient = email
        # Echoed back on every Mailgun webhook, so an event days later ties to its contact.
        variables = {"bo_object_id": self.object_id, "bo_trigger_id": self.trigger_id,
                     "bo_reference": ",".join(self.files_read),
                     "bo_technology": self.contact.get("technology", ""),
                     "bo_title": self.contact.get("title", "")}
        if self.divert:
            recipient, subject = self.divert, f"[TEST -> {email}] {subject}"
            variables |= {"bo_intended_to": email, "bo_redirected": "true"}
            t.warn(f"REDIRECT ACTIVE — this email goes to {self.divert}, NOT to the lead. "
                   f"Unset EMAIL_AGENT_REDIRECT_TO to reach real leads.")
            # This person did NOT receive the email we composed for them. That is a
            # delivery decision about an individual, so it belongs with the other
            # per-lead decisions rather than among the narration.
            t.event("redirect_active", stream=AUDIT, object_id=self.object_id,
                    to=self.divert)

        t.step(f"Deliver through {'the dry-run file' if self.dry_run else 'Mailgun'}")
        try:
            self.sent = _timed(t, "dry-run" if self.dry_run else "mailgun", "messages",
                               {"subject": subject, "redirected": bool(self.divert)},
                               lambda: self.sender.send(to=recipient, subject=subject, body=text,
                                                        variables=variables, html=html_body),
                               usage=lambda r: {"message_id": r.message_id})
        except SendError as exc:
            self.send_error = str(exc)
            t.fail(f"send refused: {exc}")
            return f"Error: the email could not be sent ({exc}). Do not retry — you are done."
        self.subject = subject
        return "Sent. You are done — do not send again."

    def _guardrail(self, body: str) -> str:
        """The role's own words must survive into the email, unaltered.

        The model is told in SKILL.md to copy `involves` phrases exactly; this is
        what makes that a rule rather than a request. It rewrote them before —
        "data extension SQL segmentation" came out as "SQL segmentation against
        data extensions" — which reads fine and is still wrong: these are the terms
        the people who hire for the role use, and consistency across a few hundred
        emails is the point.

        Returns "" to let the send proceed, or the message handed back to the model.
        One rejection only. A second failure sends anyway and is recorded: a slightly
        reworded phrase is not worth withholding a real email from a real person.
        """
        phrases, asset = involves_for(self.root, self.contact.get("technology", ""),
                                      self.contact.get("title", ""))
        if not phrases:
            return ""                       # no asset covers this lead; nothing to check
        matched = exact_phrases(body, phrases)
        missing = [p for p in phrases if p not in matched]

        if len(matched) >= GUARDRAIL_FLOOR or self.guard_rejections:
            result = "pass" if len(matched) >= GUARDRAIL_FLOOR else "failed_open"
            if result == "failed_open":
                self.t.warn(f"GUARDRAIL — only {len(matched)} phrase(s) exact after a retry; "
                            f"sending anyway and recording it")
            self.t.event("guardrail_checked", stream=AUDIT, result=result, asset=asset,
                         title=self.contact.get("title", ""), exact=len(matched),
                         floor=GUARDRAIL_FLOOR, available=len(phrases),
                         matched=matched, attempt=self.guard_rejections + 1)
            return ""

        self.guard_rejections += 1
        self.t.warn(f"GUARDRAIL — {len(matched)} of {GUARDRAIL_FLOOR} phrases exact; "
                    f"sending the draft back to the model")
        self.t.event("guardrail_checked", stream=AUDIT, result="retry", asset=asset,
                     title=self.contact.get("title", ""), exact=len(matched),
                     floor=GUARDRAIL_FLOOR, available=len(phrases),
                     matched=matched, attempt=1)
        return ("Not sent. The role's phrases must appear word for word, and "
                f"{len(matched)} of the required {GUARDRAIL_FLOOR} did. Use at least "
                f"{GUARDRAIL_FLOOR} of these exactly as written, changing nothing "
                "inside the quotes:\n"
                + "\n".join(f'  "{p}"' for p in missing)
                + "\n\nKeep your own wording everywhere else. Rewrite and call "
                  "send_email again.")

    def flag_lead(self, reason: str) -> str:
        if self.sent:
            return "Error: the email was already sent; this lead cannot be flagged now."
        self.flagged = reason.strip() or "no reason given"
        self.t.warn(f"model flagged the lead: {self.flagged}")
        return "Flagged. You are done."


ModelFn = Callable[[str, str, LeadTools], None]


def run_tool_loop(system: str, user: str, tools: LeadTools, *, model: str, max_tokens: int,
                  max_turns: int, thinking: bool, t: Trace) -> None:
    """Production model_fn: the SDK's Tool Runner drives Claude until it stops calling
    tools. One audit line per model turn."""
    import anthropic
    from anthropic import beta_tool

    @beta_tool
    def read_skill_file(path: str) -> str:
        """Read one file from the skills folder.

        Args:
            path: Path relative to the skills folder, e.g. "outreach/SKILL.md",
                "outreach/assets/salesforce.json" or "outreach/references/salesforce.md".
        """
        return tools.read_skill_file(path)

    @beta_tool
    def send_email(subject: str, body: str) -> str:
        """Send the finished email to the lead. Call exactly once, when the email is ready.

        Args:
            subject: The subject line.
            body: The plain-text body, ending with the sign-off. No footer — it is added for you.
        """
        return tools.send_email(subject, body)

    @beta_tool
    def flag_lead(reason: str) -> str:
        """Stop without sending: this lead cannot be emailed from the material available.

        Args:
            reason: One sentence saying why, e.g. "no assets entry for the title Salesforce QA".
        """
        return tools.flag_lead(reason)

    params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "max_turns": max_turns}
    try:

        runner = anthropic.Anthropic().beta.messages.tool_runner(
            model=model, max_tokens=max_tokens, max_iterations=max_turns,
            system=system, messages=[{"role": "user", "content": user}],
            tools=[read_skill_file, send_email, flag_lead],
            cache_control={"type": "ephemeral"},
            **({"thinking": {"type": "adaptive"}} if thinking else {}))
        started = time.perf_counter()
        for message in runner:
            u = message.usage
            t.outbound_call(service="anthropic", endpoint="messages.create", status=200,
                            duration_ms=round((time.perf_counter() - started) * 1000, 1),
                            params={**params, "stop_reason": message.stop_reason},
                            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
            started = time.perf_counter()
    except anthropic.APIError as exc:
        t.outbound_call(service="anthropic", endpoint="messages.create", ok=False,
                        error=str(exc), params=params)
        raise ModelError(f"model call failed ({model}): {exc}") from exc


# ------------------------------------------------------------------- agent
class EmailAgent:
    def __init__(self, sender: Sender, config: dict[str, Any], hubspot: HubSpotMCP | None = None,
                 dry_run: bool = True, model_fn: ModelFn | None = None) -> None:
        self.sender = sender
        self.config = config
        self.hs = hubspot or HubSpotMCP()
        self.dry_run = dry_run
        self.skills_root = REPO_ROOT / str(config["skill"]["path"])
        self.catalogue = skill_catalogue(self.skills_root)      # raises at startup, not per lead
        self.system_prompt = load_system_prompt(REPO_ROOT / str(config["skill"]["system_prompt"]))
        self.model_fn = model_fn or self._run_model

    def work(self, object_id: str, trigger_id: str = "", t: Trace | None = None) -> Outcome:
        """The whole pipeline for one lead."""
        t = (t or Trace.disabled()).bind(object_id=str(object_id), trigger_id=trigger_id)
        try:
            contact = self._read(object_id, t)
            self._guards(contact, object_id, t)
            divert = redirect_target()
            self._claim(object_id, divert, t)
            tools = self._model(contact, object_id, trigger_id, divert, t)
        except Skipped as stop:
            return Outcome(object_id, "skipped", str(stop))
        except Failed as stop:
            return Outcome(object_id, "failed", str(stop))

        sent = tools.sent
        t.ok(sent.detail + ("" if self.dry_run else f" — to {self.divert_or(contact, divert)}"))
        return Outcome(object_id, "sent", sent.detail, subject=tools.subject, message_id=sent.message_id,
                       reference=",".join(tools.files_read), dry_run=self.dry_run)

    @staticmethod
    def divert_or(contact: dict[str, Any], divert: str) -> str:
        return divert or (contact.get("email") or "").strip()

    # ------------------------------------------------------------- steps
    def _read(self, object_id: str, t: Trace) -> dict[str, Any]:
        t.step(f"Read contact {object_id} from HubSpot via MCP")
        asked = list(self.config["guards"]["contact_properties"])
        params = {"objectIds": [int(object_id)], "properties": asked}   # int: HubSpot rejects a quoted id
        # A HubSpotMCPError propagates: not this contact's fault, so app.py answers 503
        # and the gateway retries.
        result = _timed(t, "hubspot", "get_crm_objects", params,
                        lambda: self.hs.call("get_crm_objects", {"objectType": "CONTACT", **params}))
        props = ((result.get("objects") or [{}])[0].get("properties")) or {}
        if not props:
            t.fail(f"contact {object_id} not found in HubSpot")
            raise Failed("contact not found in HubSpot")
        present = sorted(k for k, v in props.items() if str(v or "").strip())
        empty = sorted(set(asked) - set(present))
        t.ok(f"got {len(present)} of {len(asked)} properties from {self.hs.server_url}")
        # Property NAMES only — never values.
        t.event("contact_read", server=self.hs.server_url,
                properties_returned=present, empty_or_absent=empty)
        return props

    def _guards(self, contact: dict[str, Any], object_id: str, t: Trace) -> None:
        """Four checks, in this order. Any 'no' stops the lead here."""
        t.step("Check the guards before writing anything")
        guards = self.config["guards"]
        status = (contact.get("email_status") or "").strip().upper()
        blocked = list(guards["skip_if_status_in"])

        if not (contact.get("email") or "").strip():
            t.event("guard_stopped", stream=AUDIT, guard="email", value="(empty)",
                    outcome="FAILED", reason="no email address")
            self._fail(t, object_id, "bad data: no email address")
        if status in {s.upper() for s in blocked}:
            raise self._skip(t, "email_status", status,
                             f"already {status.lower()} — this person has been contacted",
                             blocked_by=blocked)
        if (contact.get("hs_email_optout") or "").lower() == "true":
            raise self._skip(t, "hs_email_optout", "true", "contact opted out of all email in HubSpot")
        if guards.get("require_decision_maker") and (contact.get("decision_maker") or "").lower() != "true":
            raise self._skip(t, "decision_maker", contact.get("decision_maker") or "(empty)",
                             "decision_maker is no longer true")

        t.ok("has an email address, not contacted before, not opted out, still a decision maker")
        t.event("guards_passed", stream=AUDIT, email_status=status or "(empty)",
                hs_email_optout=contact.get("hs_email_optout") or "(empty)",
                decision_maker=contact.get("decision_maker") or "(empty)",
                require_decision_maker=bool(guards.get("require_decision_maker")),
                skip_if_status_in=blocked)

    @staticmethod
    def _skip(t: Trace, guard: str, value: str, reason: str, **fields: Any) -> Skipped:
        t.warn(f"STOPPING: {reason} — no email will be written")
        t.event("guard_stopped", stream=AUDIT, guard=guard, value=value,
                outcome="skipped", **fields)
        return Skipped(reason)

    def _fail(self, t: Trace, object_id: str, reason: str) -> None:
        """FAILED in HubSpot with the reason, then stop. Nobody is dropped silently."""
        t.fail(reason)
        self._status(object_id, FAILED, t)
        raise Failed(reason)

    def _claim(self, object_id: str, divert: str, t: Trace) -> None:
        """email_status=SENT BEFORE the model runs, so a gateway retry mid-compose cannot
        send a second email. A redirected send is a test: the lead received nothing and
        must stay contactable, so it is not claimed."""
        t.step("Claim this lead in HubSpot (email_status = SENT)")
        if divert:
            t.warn("SKIPPED — redirected test send; this lead must stay contactable")
            t.event("hubspot_write_skipped", stream=AUDIT, object_id=object_id, would_write=SENT,
                    reason="redirect active: the lead receives nothing")
        else:
            self._status(object_id, SENT, t)

    def _model(self, contact: dict[str, Any], object_id: str, trigger_id: str,
               divert: str, t: Trace) -> LeadTools:
        """Hand the lead to the model. It reads the skill, writes, and sends — through tools."""
        model = self.config["model"]
        t.step(f"Hand the lead to {model['name']} with the skill catalogue and three tools")
        tools = LeadTools(contact, object_id, trigger_id, self.sender, self.config["sender"],
                          self.skills_root, divert, self.dry_run, t)
        system = render_system_prompt(self.system_prompt, self.catalogue, self.config["sender"])
        user = lead_message(contact)
        prompt_file = write_transcript(t.trace_id, "System prompt + lead message",
                                       f"{system}\n\n---\n\n{user}")
        t.event("prompt_built",
                agent_md=_rel(self.system_prompt.path), agent_md_sha=self.system_prompt.sha,
                skills=[f"{_rel(s.path)}@{s.sha}" for s in self.catalogue],
                prompt_chars=len(system) + len(user), prompt_sha=digest(system + user),
                prompt_file=prompt_file)
        try:
            self.model_fn(system, user, tools)
        except ModelError as exc:
            self._fail(t, object_id, f"compose: {exc}")
        t.event("model_done", files_read=tools.files_read, sent=bool(tools.sent),
                flagged=tools.flagged, reply_file=_rel(transcript_path(t.trace_id)))
        if tools.flagged:
            self._fail(t, object_id, f"flagged by model: {tools.flagged}")
        if tools.send_error:
            self._fail(t, object_id, f"send: {tools.send_error}")
        if not tools.sent:
            self._fail(t, object_id, "model finished without sending or flagging the lead")
        return tools

    def _run_model(self, system: str, user: str, tools: LeadTools) -> None:
        m = self.config["model"]
        run_tool_loop(system, user, tools, model=m["name"], max_tokens=int(m.get("max_tokens", 4096)),
                      max_turns=int(m.get("max_turns", 8)),
                      thinking=str(m.get("thinking", "adaptive")).lower() != "off", t=tools.t)

    def _status(self, object_id: str, status: str, t: Trace) -> None:
        """Write email_status. Best effort: a failed write is logged, never raised."""
        if self.dry_run:
            t.warn(f"SKIPPED — dry run writes nothing to HubSpot (would write {status})")
            t.event("hubspot_write_skipped", stream=AUDIT, object_id=object_id, would_write=status,
                    reason="dry run writes nothing to HubSpot")
            return
        params = {"objectId": int(object_id), "email_status": status}
        try:
            _timed(t, "hubspot", "manage_crm_objects", params,
                   lambda: self.hs.call("manage_crm_objects", {
                       "confirmationStatus": "CONFIRMED",
                       "updateRequest": {"objects": [{
                           "objectType": "contacts", "objectId": int(object_id),
                           "properties": {"email_status": status}}]}}))
        except HubSpotMCPError:
            return
        t.event("status_written", stream=AUDIT, email_status=status)


# ------------------------------------------------------------------ footer
def with_footer(body: str, sender: dict[str, str], unsubscribe: str) -> str:
    """Plain-text part. Written by code, never by the model: CAN-SPAM requires a
    postal address and an unsubscribe link in every commercial email."""
    company = sender.get("company", "").strip()
    address = sender.get("postal_address", "").strip()
    return f"{body.rstrip()}\n\n--\n{company}\n{address}\nUnsubscribe: {unsubscribe}"


def as_html(body: str, sender: dict[str, str], unsubscribe: str) -> str:
    """HTML part, so the unsubscribe link can be a word rather than a 250-char token.
    Deliberately plain. Model output is escaped before it goes anywhere near markup."""
    company = sender.get("company", "").strip()
    address = sender.get("postal_address", "").strip()
    paragraphs = "\n\n".join(
        f"<p>{html.escape(block.strip()).replace(chr(10), '<br>')}</p>"
        for block in body.strip().split("\n\n") if block.strip())
    footer_line = " &middot; ".join(x for x in (html.escape(company), html.escape(address)) if x)
    return (f"{paragraphs}\n\n"
            f'<p style="font-size:11px;color:#888;">\n'
            f"  {footer_line}<br>\n"
            f"  You received this because we believe it&#39;s relevant to your work.\n"
            f'  <a href="{unsubscribe}">Unsubscribe</a>.\n'
            f"</p>")
