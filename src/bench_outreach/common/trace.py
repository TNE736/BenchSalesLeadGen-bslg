"""Logs a person can read.

Every service writes the same two things:

  * **console** — the story of one lead, step by step, boxed so runs never interleave
    into nonsense. Someone who has never seen this code should be able to read a
    terminal and say what happened and where it stopped.
  * **file** — the same events as JSON lines, one per line, for grepping and later
    analysis. `logs/<service>/<service>.jsonl`.

Both carry the same `run_id`, and every hop carries the `trigger_id` the gateway
minted, so one lead can be followed across two services and two files.

Set `BENCH_LOG_FORMAT=json` to make the console emit JSON too (for Cloud Run and
friends, where a log collector reads stdout and nobody is watching).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import env

# Box drawing, with an ASCII fallback for terminals that mangle UTF-8.
_UNICODE = (sys.stdout.encoding or "").lower().startswith("utf")
TOP, MID, END = ("┌─", "│ ", "└─") if _UNICODE else ("+-", "| ", "+-")
ARROW = "→" if _UNICODE else "->"
OK, FAIL, WARN, INFO = ("✓", "✗", "!", "·") if _UNICODE else ("[ok]", "[x]", "[!]", "-")
INDENT = "  ↳ " if _UNICODE else "    > "
#: Printed only on the streams that are the exception. `process` is the rule —
#: a tag repeated on nine lines in ten is noise, not information, so it is left
#: off and declared once in the run header instead. The JSON still carries
#: `stream` on every record; this is about what a person reads.
OUT = "⟶" if _UNICODE else "=>"


# ── redaction ───────────────────────────────────────────────────────────────
# Runs inside _record, so nothing written can go round it. Two rules, and they
# work differently on purpose.
#
# CREDENTIALS are caught by the KEY. A field called `api_key` holds one whatever
# its value looks like.
#
# PERSONAL DATA is caught by the VALUE, not the key — and that difference is the
# whole design. Our most useful log line is
#
#     "properties_returned": ["email", "firstname", "mobilephone", ...]
#
# which is a list of HubSpot property NAMES and is exactly what proves what we
# read. A redactor keyed on the word "email" would blank it and leave a line
# that proves nothing. LQABR's research agent hit that trap and their code still
# carries the scar in a comment. So: the string "email" survives, the string
# "v@example.com" does not.

#: Field names whose VALUE is a credential, whatever it looks like.
_SECRET_KEYS = ("token", "secret", "password", "api_key", "apikey", "authorization",
                "auth", "bearer", "credential", "signing_key", "private_key")

#: ...except these NAME a credential rather than holding one. Logging which key
#: was used is the point of those lines; blanking it is the inverse of the rule.
_NAMES_A_SECRET = ("_name", "_source", "_ref", "_sha", "_file", "_digest")

#: A token COUNT is a number the log exists to show, not a token.
_SAFE_KEYS = ("tokens", "max_tokens", "input_tokens", "output_tokens",
              "total_tokens", "token_count")

#: Field names whose VALUE is an identifier we log ON PURPOSE. Never scrubbed,
#: however personal the value may look. The Mailgun message id is why this exists:
#:
#:     <20260919093735.358fb3dc6e278b9d@reply.tekninjas.com>
#:
#: it ends in a domain, so the email pattern below eats the whole thing — and it
#: is the ONLY key that ties a delivered / opened / bounced / unsubscribed webhook
#: back to the run that sent the mail. Redacting it does not protect anyone; it
#: just makes delivery tracking impossible.
#:
#: Exact matches only, no suffix matching: a new id field is added here on
#: purpose, so nobody widens the hole by naming a variable `..._id`.
_IDENTIFIER_KEYS = ("message_id", "run_id", "trigger_id", "object_id")

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
#: A run of digits carrying phone punctuation. Matched loosely, then checked:
#: seven or more actual digits AND at least one separator or a leading plus.
#: That second condition is what spares a bare HubSpot object id — 553374486245
#: is twelve digits with no punctuation, it is logged deliberately, and it is the
#: id a person searches by.
_PHONE = re.compile(r"\+?\d[\d\s().-]{5,}\d")
#: ...but a date carries digits and dashes too, so the loose pattern above matches
#: one. `drafts-2026-09-18.md` became `drafts-<redacted>.md` until this was added.
#: A log that loses its dates loses the thing people search it by.
_DATEISH = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")

REDACTED = "<redacted>"

# --------------------------------------------------------------------- streams
# Every record carries exactly one of these. The question each answers is
# different, which is why they are separate rather than one log with a level:
#
#   SYSTEM   the SERVICE. Startup, configuration, what was loaded, shutdown.
#            True before any lead exists and after the last one is done. If it
#            would still be written on a day nobody was emailed, it is system.
#
#   PROCESS  the WORK, for one lead. The steps, the guards, the skill chosen,
#            the draft. Answers "what did we decide, and why".
#
#   AUDIT    the CALLS that left this process. HubSpot, Anthropic, Mailgun:
#            endpoint, status, duration, tokens. Answers "what did it cost".
#
# The line that keeps them apart: audit records what a call COST, process records
# what it PRODUCED, system records what the service IS. So token counts are audit,
# the email's subject is process, and the loaded skill's sha is system.
SYSTEM, PROCESS, AUDIT = "system", "process", "audit"
STREAMS = (SYSTEM, PROCESS, AUDIT)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SAFE_KEYS or lowered.endswith(_NAMES_A_SECRET):
        return False
    return any(hint in lowered for hint in _SECRET_KEYS)


def _looks_like_a_phone(text: str) -> bool:
    if _DATEISH.fullmatch(text.strip()):
        return False
    return (sum(c.isdigit() for c in text) >= 7
            and (text.startswith("+") or any(c in " ().-" for c in text)))


def _scrub(value: Any, depth: int = 0) -> Any:
    """Blank personal data found in a VALUE, however deeply it is nested."""
    if depth > 6:
        return value
    if isinstance(value, str):
        cleaned = _EMAIL.sub(REDACTED, value)
        cleaned = _PHONE.sub(
            lambda m: REDACTED if _looks_like_a_phone(m.group()) else m.group(),
            cleaned)
        return cleaned
    if isinstance(value, dict):
        return redact(value, depth + 1)
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value]
    return value


def redact(fields: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Every field bag passes through this on its way to a log line."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key).lower()
        if _is_secret_key(name):
            out[key] = REDACTED
        elif name in _IDENTIFIER_KEYS:
            out[key] = value                    # an id, logged on purpose
        else:
            out[key] = _scrub(value, depth)
    return out


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Trace:
    """One run's log. Use as a context manager so the box always closes."""

    def __init__(self, service: str, run_id: str, title: str,
                 log_dir: Path | None = None) -> None:
        self.service = service
        self.run_id = run_id
        self.started = time.perf_counter()
        self._log = _logger(service, log_dir)
        self._json_console = env("BENCH_LOG_FORMAT").lower() == "json"
        self._bound: dict[str, Any] = {}
        self._steps = 0
        self._console(f"{TOP} {title}   run {run_id}")
        self._console(f"{MID} unmarked = process (work on this lead)   "
                      f"{OUT} AUDIT = a call that left us   {OUT} SYSTEM = the service")
        self._record("run_started", title=title)

    def bind(self, **fields: Any) -> "Trace":
        """Attach fields to every event from here on.

        So `object_id` and `trigger_id` appear on all of a run's lines, not just
        the ones that happened to pass them. One `grep 551358867185` then returns
        the whole story of that lead rather than three of its twelve events.
        """
        self._bound.update({k: v for k, v in fields.items() if v not in (None, "")})
        return self

    # ------------------------------------------------------------------ steps
    def step(self, label: str, **fields: Any) -> None:
        """The next numbered stage of the flow.

        The run numbers its own steps. They used to be written in by hand, which
        meant the flow could only be as detailed as the agent's own five calls: a
        stage that happened inside compose() could not be a step, because the
        numbers lived in agent.py. Now anything that happens can announce itself,
        and inserting a stage does not renumber the ones after it by hand.
        """
        self._steps += 1
        if self._steps > 1:
            self._console(MID.rstrip())         # breathing room between stages
        self._console(f"{MID}STEP {self._steps}  {label}")
        self._record("step", step=self._steps, label=label, **fields)

    def ok(self, text: str, **fields: Any) -> None:
        self._console(f"{MID}   {OK} {text}")
        self._record("ok", detail=text, **fields)

    def fail(self, text: str, **fields: Any) -> None:
        self._console(f"{MID}   {FAIL} {text}")
        self._record("fail", detail=text, **fields)

    def warn(self, text: str, **fields: Any) -> None:
        self._console(f"{MID}   {WARN} {text}")
        self._record("warn", detail=text, **fields)

    def detail(self, text: str, **fields: Any) -> None:
        """An indented continuation line under the step above it."""
        self._console(f"{MID}{INDENT}{text}")
        if fields:
            self._record("detail", detail=text, **fields)

    def event(self, name: str, stream: str = PROCESS, **fields: Any) -> None:
        """A machine-readable event with no console line of its own.

        For the things a person does not need narrated but an auditor does: the
        model call, a HubSpot write, the skill and asset actually used. Before
        this existed, services kept a second `_emit` helper beside Trace and every
        event had to be written twice, in two styles, at two call sites.
        """
        self._record(name, stream=stream, **fields)

    def system(self, name: str, text: str = "", **fields: Any) -> None:
        """A fact about the SERVICE, not about any one lead.

        What was loaded at startup, what the configuration resolved to, why the
        agent refused to start. These used to be written as ordinary process
        events, which made "is this service healthy?" and "what happened to this
        lead?" the same query.
        """
        if text:
            self._console(f"{MID}{OUT} SYSTEM  {text}", SYSTEM)
        self._record(name, stream=SYSTEM, detail=text, **fields)

    def outbound_call(self, *, service: str, endpoint: str, ok: bool = True,
                      status: int | None = None, duration_ms: float | None = None,
                      params: dict[str, Any] | None = None,
                      error: str = "", **usage: Any) -> None:
        """One call that LEFT this process, on the audit stream.

        Every hop uses this shape — HubSpot, Anthropic, Mailgun — so "show me
        every external call this run made" is one grep instead of three event
        names. Before it, the same three concepts had three different field
        layouts.

        `params` is what we SENT: the tool and its arguments, the model and its
        knobs. Summarised, and redacted on the way out like everything else.
        Without it the log says a call happened but not what it asked for, which
        is the question a person actually has.

        `usage` is what the call COST — token counts and the like. The line that
        keeps this from becoming a dumping ground: **audit records what the call
        cost, process records what the call produced.** So tokens belong here and
        the subject line of the email does not.
        """
        cost = f"{duration_ms / 1000:.1f}s" if duration_ms is not None else "-"
        tokens = ""
        if usage.get("input_tokens"):
            tokens = f"  {usage['input_tokens']} in / {usage.get('output_tokens', 0)} out"
        self._console(f"{MID}{OUT} AUDIT   {service} {endpoint}  "
                      f"{status if ok else 'FAILED'}  {cost}{tokens}"
                      + (f"  {error}" if error else ""), AUDIT)
        self._record("outbound_call", stream=AUDIT, service=service,
                     endpoint=endpoint, ok=ok, status=status,
                     duration_ms=duration_ms, params=params or {},
                     error=error, **{k: v for k, v in usage.items() if v is not None})

    def end(self, summary: str, **fields: Any) -> None:
        elapsed = time.perf_counter() - self.started
        self._console(f"{END} {summary}   ({elapsed:.1f}s)")
        if not self._json_console:
            print(flush=True)                  # a blank line, not a tagged one
        self._record("run_finished", summary=summary,
                     duration_ms=round(elapsed * 1000, 1), **fields)

    # -------------------------------------------------------------- plumbing
    def _console(self, line: str, stream: str = PROCESS) -> None:
        """Print one line, tagged with the stream it belongs to.

        The tag is not decoration. Reading a terminal you could not tell a decision
        our code made (process) from a call that left it (audit) from a fact about
        the service (system) — they all looked like narration. The tag is the same
        word that goes in the JSON, so what you read live and what you grep later
        are the same thing.
        """
        if self._json_console:
            return                      # the JSON record is written separately
        print(f"{_clock()} {line}", flush=True)

    def _record(self, event: str, stream: str = PROCESS, **fields: Any) -> None:
        payload = {"ts": int(time.time() * 1000), "service": self.service,
                   "run_id": self.run_id, "stream": stream, "event": event,
                   **redact({**getattr(self, "_bound", {}), **fields})}
        line = json.dumps(payload, default=str)
        self._log.info(line)
        if self._json_console:
            print(line, flush=True)

    @classmethod
    def disabled(cls) -> "Trace":
        """A Trace that discards everything — for tests and for library use."""
        return _Silent()

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.fail(f"unhandled {exc_type.__name__}: {exc}")
            self.end("FAILED")


class _Silent(Trace):
    """A Trace that writes nothing.

    Exists so callers can hold a Trace unconditionally instead of guarding every
    log line with `if t:`. Fourteen of those guards used to live in the email
    agent's `work()`.
    """

    def __init__(self) -> None:                    # noqa: D107 - deliberately not super()
        self.service, self.run_id = "", ""
        self.started = time.perf_counter()
        self._json_console = False
        self._bound = {}
        self._steps = 0

    def _console(self, line: str, stream: str = PROCESS) -> None:
        pass

    def _record(self, event: str, stream: str = "process", **fields: Any) -> None:
        pass


def _logger(service: str, log_dir: Path | None) -> logging.Logger:
    """File-only logger: the console is written by Trace itself, in human form."""
    log = logging.getLogger(f"bench_outreach.trace.{service}")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    from .settings import REPO_ROOT
    directory = log_dir or (REPO_ROOT / "logs" / service)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(directory / f"{service}.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    except OSError:
        log.addHandler(logging.NullHandler())
    return log


def banner(service: str, lines: list[str]) -> None:
    """Printed once at startup, so a terminal says what it is and how it is configured."""
    if env("BENCH_LOG_FORMAT").lower() == "json":
        return
    width = max(len(l) for l in [service] + lines) + 4
    bar = "=" * width
    print(f"\n{bar}\n  {service}\n{bar}")
    for line in lines:
        print(f"  {line}")
    print(f"{bar}\n", flush=True)
