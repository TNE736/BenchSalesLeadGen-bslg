"""Logging Spec v3.0 — the whole of it, once.

Two services live in this repo and the spec asks for `<service>_logging` per
service (§10). Each service HAS one -- `gateway/gateway_logging.py`,
`email_agent/email_agent_logging.py` -- and each is what a developer imports
and reads. They share this engine rather than carrying a copy each, because
two copies of seven hundred lines is two things to drift apart, which is the
thing a spec exists to prevent. Declared in the report.

Everything the spec fixes is fixed here: the record (§2), the three files and
their rotation (§1), the console (§1.1), other libraries silenced (§1.2), the
trace id (§3), steps (§4), external calls (§5), system events (§6), redaction
(§7), the modes (§8), and never failing (§9).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ------------------------------------------------------------------ §1 files
PROCESS, AUDIT, SYSTEM = "process", "audit", "system"
STREAMS = (PROCESS, AUDIT, SYSTEM)
FILES = {s: f"{s}.log" for s in STREAMS}

MAX_BYTES = 50 * 1024 * 1024
BACKUPS = 5

TERSE, NORMAL, DEBUG = "terse", "normal", "debug"
MODES = (TERSE, NORMAL, DEBUG)
CUT = 240

REDACTED = "<redacted>"

#: §3 -- Input 2. Set where the work starts, read by every line, restored when
#: the work ends. `step` rides the same store (§2): step_in sets it, step_out
#: puts back what was there.
_TRACE: ContextVar[str] = ContextVar("trace_id", default="")
_STEP: ContextVar[str] = ContextVar("step", default="")
_ITEM: ContextVar[tuple[int, int] | None] = ContextVar("item", default=None)
#: §4 -- the tally of the step currently iterating, so `step_out` can end
#: with the count by status and the keys that did not succeed.
_TALLY: ContextVar["Tally | None"] = ContextVar("tally", default=None)


def trace_id() -> str:
    return _TRACE.get()


def new_trace_id() -> str:
    """§3 -- 16 random bytes as 32 lowercase hex. Born where the work starts."""
    return secrets.token_hex(16)


def traceparent() -> dict[str, str]:
    """§3 -- how the id crosses in a call's metadata."""
    current = _TRACE.get()
    return ({"traceparent": f"00-{current}-{secrets.token_hex(8)}-01"} if current else {})


_TP = re.compile(r"^00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def from_traceparent(header: Any) -> str:
    """The 32-hex from a `traceparent`, or `""` when missing or malformed."""
    match = _TP.match(str(header or "").strip())
    return match.group(1) if match and match.group(1) != "0" * 32 else ""


def valid_trace_id(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 32
            and all(c in "0123456789abcdef" for c in value) and value != "0" * 32)


# -------------------------------------------------------------- §7 redaction
_KEEP_EXACT = frozenset(("max_tokens", "input_tokens", "output_tokens",
                         "total_tokens", "token_count"))
#: §7's four suffixes, plus two the spec needs elsewhere: §5 requires the
#: provider's usage counters BY NAME -- `cache_read_tokens`, `web_search_requests`
#: -- and §7 rule 3 would redact both. A counter is not a credential. Declared
#: in the report as decided without the spec, since the two sections conflict.
_KEEP_SUFFIX = ("_name", "_source", "_ref", "_kind", "_type", "_count",
                "_tokens", "_requests")
_SECRET = ("token", "secret", "password", "passwd", "api_key", "apikey",
           "authorization", "auth", "bearer", "credential", "private_key",
           "access_key", "session_id", "cookie", "signature")


def redact(value: Any, key: str = "") -> Any:
    """§7 -- before every line, at every depth. First match wins."""
    name = str(key).lower()
    if name:
        if name in _KEEP_EXACT:
            return value
        if not name.endswith(_KEEP_SUFFIX) and any(s in name for s in _SECRET):
            return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


# ------------------------------------------------------------------ §8 modes
def cut(text: str, limit: int = CUT) -> str:
    """The cut form, exactly: `<first N chars> ... (+M chars)`."""
    return text if len(text) <= limit else f"{text[:limit]} ... (+{len(text) - limit} chars)"


def shorten(value: Any, mode: str, limit: int = CUT) -> Any:
    """Strings cut at `limit`, unless the mode is debug -- where nothing is
    cut and nothing is summarised: a value is whole, or absent."""
    if mode == DEBUG:
        return value
    if isinstance(value, str):
        return cut(value, limit)
    if isinstance(value, dict):
        return {k: shorten(v, mode, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [shorten(v, mode, limit) for v in value]
    return value


def keys_only(payload: Any) -> dict[str, Any]:
    return {"keys": sorted(map(str, payload))} if isinstance(payload, dict) else {"keys": []}


# ---------------------------------------------------------------- §2 record
def now() -> str:
    """ISO 8601, UTC, milliseconds, Z."""
    at = datetime.now(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


@dataclass
class Settings:
    """§1 -- three settings, read once. Environment beats config file beats
    default, and §1 forbids pinning any of them in a checked-in env file."""

    service: str
    log_dir: Path | None
    mode: str
    version: str = "0.1.0"
    problems: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def read(cls, service: str, folder: str | None = None, mode: str | None = None,
             version: str = "0.1.0") -> "Settings":
        problems: list[dict[str, Any]] = []
        service = os.environ.get("SERVICE_NAME") or service
        raw_dir = os.environ.get("LOG_DIR", folder if folder is not None else "logs")
        raw_mode = (os.environ.get("LOG_MODE") or mode or NORMAL).strip().lower()
        if raw_mode not in MODES:
            problems.append({"setting": "LOG_MODE", "given": raw_mode, "used": NORMAL})
            raw_mode = NORMAL
        return cls(service=service, log_dir=Path(raw_dir) if raw_dir else None,
                   mode=raw_mode, version=version, problems=problems)


# --------------------------------------------------------------- §1.1 console
#: The column widths of §1.1. The content column starts here, and continuation
#: lines are indented to it.
W_TIME, W_STREAM, W_MARK, W_KIND, W_STEP = 8, 7, 2, 4, 15
CONTENT_AT = W_TIME + 1 + W_STREAM + 1 + W_MARK + 1 + W_KIND + 1 + W_STEP + 1
INDENT = " " * CONTENT_AT

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:                                    # noqa: BLE001
        return False


def _paint(text: str, colour: str, colour_on: bool) -> str:
    return f"{colour}{text}{OFF}" if colour_on else text


def _pairs(fields: Any) -> str:
    """`k=v` for the console. Empty and null are not shown; booleans are
    true/false; an array over 3 items shows `[N]` (§1.1)."""
    if not isinstance(fields, dict):
        return _one(fields)
    out = []
    for k, v in fields.items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        out.append(f"{k}={_one(v)}")
    return "  ".join(out)


def _one(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)}]" if len(value) > 3 else "[" + ", ".join(_one(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + _pairs(value) + "}"
    return str(value)


_GLYPH = {"ok": "+", "failed": "x", "skipped": "!"}


def console_lines(record: dict[str, Any], colour: bool) -> list[str]:
    """One record, rendered for a person -- from the SAME record object the
    file gets, never a second code path (§1.1)."""
    event = str(record.get("event", ""))
    if event == "inbound_request":
        return []                    # §1.2 -- route and method are on run_start

    stream = str(record.get("stream", PROCESS))
    step = str(record.get("step", "") or "")
    item, of = record.get("item"), record.get("of")
    if step and item and of:
        step = f"{step} {item}/{of}"
    if not step:
        #: §1.1's example reads `*  run_start`, `*  service_start` -- with no
        #: step of its own, a record names itself in that column.
        step = event

    mark, kind, content, extra = "*", "", "", []

    if event == "step_in":
        mark, kind = ">", "IN"
        pairs = _pairs(record.get("inputs") or {})
        content = ("inputs: " + pairs) if pairs else ""
    elif event == "step_out":
        mark, kind = "<", "OUT"
        content = _outcome(record, colour)
    elif event == "item_start":
        mark, kind = ">", "IN"
        content = _pairs({k: v for k, v in record.items() if k not in _ALWAYS_SHOWN})
    elif event == "item_done":
        mark, kind = "<", "OUT"
        content = _outcome(record, colour)
    elif event == "outbound_call":
        mark, kind = "->", "CALL"
        content, extra = _call(record)
    elif event == "run_start":
        pairs = _pairs(record.get("inputs") or {})
        content = f"{record.get('trigger', '')}" + (f"  inputs: {pairs}" if pairs else "")
    elif event == "run_end":
        content = _outcome(record, colour)
    else:
        #: Anything else: its fields, all of them -- it has no columns to hide in.
        content = _pairs({k: v for k, v in record.items() if k not in _ALWAYS_SHOWN})

    head = (f"{_clock(record):<{W_TIME}} {stream:<{W_STREAM}} "
            f"{_paint(mark, DIM, colour and mark == '->'):<{W_MARK + (len(DIM + OFF) if colour and mark == '->' else 0)}} "
            f"{kind:<{W_KIND}} {step:<{W_STEP}} ")
    tail = _paint(f"trace_id={record.get('trace_id', '')}", DIM, colour)
    lines = [f"{head}{content}   {tail}"]
    lines += [INDENT + e for e in extra]
    return lines


#: On the console these are the line's own columns, or are shown by name in
#: the event branch above -- never repeated as `k=v`.
#: On the console these are the line's own columns, or are rendered by the
#: event's own branch -- never repeated as `k=v`. A `system` event is NOT in
#: this list's scope: it has no columns of its own, so every field it carries
#: is shown (§1.1, "any other event -- the event name, then its fields").
_META = {"ts", "trace_id", "stream", "service", "event", "step", "item", "of",
         "status", "duration_ms", "reason", "inputs", "outputs", "trigger",
         "peer", "kind", "operation", "endpoint", "method", "ok", "attempt",
         "credential_name", "request", "response", "error", "traceback"}
_ALWAYS_SHOWN = {"ts", "trace_id", "stream", "service", "event", "step", "item", "of"}


def _clock(record: dict[str, Any]) -> str:
    try:
        at = datetime.strptime(str(record["ts"]), "%Y-%m-%dT%H:%M:%S.%fZ")
        return at.replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")
    except Exception:                                    # noqa: BLE001
        return "--:--:--"


def _outcome(record: dict[str, Any], colour: bool) -> str:
    """`+ ok` / `x failed` / `! skipped`, ms, then outputs. `reason=` first
    when failed or skipped (§1.1)."""
    status = str(record.get("status", ""))
    glyph = _GLYPH.get(status, "*")
    tint = {"+": GREEN, "x": RED, "!": YELLOW}.get(glyph, "")
    out = f"{_paint(glyph, tint, colour and bool(tint))} {status}"
    if record.get("duration_ms") is not None:
        out += f"  {record['duration_ms']:.0f} ms"
    bits = []
    if record.get("reason"):
        bits.append(f"reason={record['reason']}")
    rest = _pairs(record.get("outputs") or {})
    if rest:
        bits.append("outputs: " + rest)
    extra = _pairs({k: v for k, v in record.items() if k not in _META})
    if extra:
        bits.append(extra)
    return out + ("  " + "  ".join(bits) if bits else "")


def _call(record: dict[str, Any]) -> tuple[str, list[str]]:
    """`<kind> <operation>  <peer><path>  <status>  <ms>  <size | tokens>`.
    The operation comes first so a run of MCP calls reads by tool name."""
    kind = str(record.get("kind", ""))
    endpoint = str(record.get("endpoint", ""))
    if "://" in endpoint:                       # a whole URL: its path, not its host
        endpoint = "/" + endpoint.split("://", 1)[1].partition("/")[2]
        endpoint = "" if endpoint == "/" else endpoint
    where = f"{record.get('peer', '')}{endpoint if kind != 'model' else ''}"
    status = record.get("status")
    line = (f"{kind} {record.get('operation', '')}  {where}  "
            f"{status if record.get('ok') else 'FAILED'}  "
            f"{record.get('duration_ms', 0):.0f} ms")

    response = record.get("response") or {}
    if kind == "model" and isinstance(response, dict):
        tokens = f"{response.get('input_tokens', 0)}→{response.get('output_tokens', 0)} tok"
        line += f"  {tokens}"
    elif isinstance(response, dict) and response.get("bytes") is not None:
        line += f"  {response['bytes']:,} bytes"
        if response.get("content_type"):
            line += f"  {response['content_type']}"
    if record.get("error"):
        line += f"  {record['error']}"

    extra: list[str] = []
    if kind == "model":
        #: §1.1 -- a model call adds two indented lines: what was asked and
        #: what came back. Cut per mode, already, by the emitter.
        request = record.get("request") or {}
        if isinstance(request, dict) and request.get("prompt"):
            extra.append("prompt:   " + _flat(request["prompt"]))
        if isinstance(response, dict) and response.get("text"):
            extra.append("response: " + _flat(response["text"]))
    return line, extra


def _flat(text: Any) -> str:
    return " ".join(str(text).splitlines())


# ------------------------------------------------------------------ the logger
class Logger:
    """One service's logging. §9: it catches its own errors; the work never
    stops because logging did."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.service = settings.service
        self.mode = settings.mode
        self._dir: Path | None = None
        self._said: set[str] = set()          # log_write_failed, once per file
        #: Two decisions, not one. `_console` is §1.1's "if stdout is not a
        #: terminal, write the JSON line instead" -- the FORMAT. `_colour` is
        #: §1.1's "never on a non-terminal" -- the ANSI codes. Held together in
        #: one flag, turning colour off also turned the rendering back to JSON.
        self._console = _tty()
        self._colour = self._console and not os.environ.get("NO_COLOR", "")
        self.run_tally = Tally()
        self._open()
        for bad in settings.problems:
            self.system("config_invalid", **bad)

    # ------------------------------------------------------------- §1 files
    def _open(self) -> None:
        if self.settings.log_dir is None:
            return                            # empty setting -> standard output
        try:
            self.settings.log_dir.mkdir(parents=True, exist_ok=True)
            self._dir = self.settings.log_dir
        except OSError as exc:
            self._dir = None
            self.system("log_write_failed", file=str(self.settings.log_dir),
                        reason=f"{type(exc).__name__}: {exc}")

    def _to_file(self, stream: str, line: str) -> None:
        """The file, if there is one. Standard output is the caller's job, and
        exactly once: writing here as well is how every record came out twice
        when the folder was unwritable."""
        if self._dir is None:
            return
        path = self._dir / FILES[stream]
        try:
            self._rotate(path)
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            if str(path) not in self._said:
                self._said.add(str(path))
                self.system("log_write_failed", file=str(path),
                            reason=f"{type(exc).__name__}: {exc}")

    def _rotate(self, path: Path) -> None:
        """50 MB, keep 5. The live file keeps its name."""
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        oldest = path.with_name(f"{path.name}.{BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for n in range(BACKUPS - 1, 0, -1):
            older = path.with_name(f"{path.name}.{n}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{n + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))

    def _stdout(self, line: str) -> None:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:                                # noqa: BLE001
            pass

    # ------------------------------------------------------------ §2 record
    def emit(self, stream: str, event: str, **fields: Any) -> None:
        try:
            record: dict[str, Any] = {
                "ts": now(), "trace_id": _TRACE.get(), "stream": stream,
                "service": self.service, "event": event,
            }
            step = _STEP.get()
            if step:
                record["step"] = step
                item = _ITEM.get()
                if item:
                    record["item"], record["of"] = item
            for key, value in redact(fields).items():
                if key not in record:
                    record[key] = value

            line = json.dumps(record, default=str)
            self._to_file(stream, line)
            #: §1.1 -- the terminal shows the same records as the files, from the
            #: same record object. If stdout is not a terminal, the JSON line
            #: instead. Either way, once.
            if self._console:
                for text in console_lines(record, self._colour):
                    self._stdout(text)
            else:
                self._stdout(line)
        except Exception:                                # noqa: BLE001 -- §9
            pass

    def system(self, event: str, **fields: Any) -> None:
        self.emit(SYSTEM, event, **fields)

    # -------------------------------------------------------------- §4 steps
    @contextmanager
    def run(self, trigger: str, *, adopt: str = "", **inputs: Any) -> Iterator[str]:
        """One run. §3: the id is born here unless one was handed to us."""
        current = adopt if valid_trace_id(adopt) else new_trace_id()
        token, step_token = _TRACE.set(current), _STEP.set("")
        self.run_tally = Tally()
        started = time.perf_counter()
        self.emit(PROCESS, "run_start", trigger=trigger, inputs=self._io(inputs))
        state: dict[str, Any] = {"status": "ok", "outputs": {}, "reason": ""}
        try:
            yield current
        except Exception as exc:
            state["status"], state["reason"] = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            #: §4 -- a run that iterated items ends with the count by status
            #: and the keys of the ones that did not succeed.
            produced = dict(self.run_outputs)
            if self.run_tally.total:
                produced.update(self.run_tally.counts())
            ended = {"status": state["status"],
                     "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                     "outputs": self._io(produced)}
            if state["reason"]:
                ended["reason"] = state["reason"]
            self.emit(PROCESS, "run_end", **ended)
            self.run_outputs, self.run_tally = {}, Tally()
            _TRACE.reset(token)
            _STEP.reset(step_token)

    run_outputs: dict[str, Any] = {}

    def finish(self, **outputs: Any) -> None:
        """What the run produced, for `run_end`."""
        self.run_outputs = {**self.run_outputs, **outputs}

    @contextmanager
    def step(self, name: str, **inputs: Any) -> Iterator["Step"]:
        """§4 -- `step_out` exactly once per `step_in`: on return, on early
        return, and on exception (failed, reason, re-raise)."""
        token = _STEP.set(name)
        tally = Tally()
        tally_token = _TALLY.set(tally)
        started = time.perf_counter()
        self.emit(PROCESS, "step_in", inputs=self._io(inputs))
        frame = Step(name)
        try:
            yield frame
        except Exception as exc:
            frame.status = "failed"
            frame.reason = f"{type(exc).__name__}: {exc}"
            if self.mode == DEBUG:
                import traceback
                frame.traceback = traceback.format_exc()
            raise
        finally:
            #: §4 -- a step that iterated items ends with the count by status
            #: and the keys of the ones that did not succeed.
            produced = dict(frame.outputs)
            if tally.total:
                produced.update(tally.counts())
            done: dict[str, Any] = {
                "status": frame.status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "outputs": self._io(produced),
            }
            if frame.reason:
                done["reason"] = frame.reason
            if frame.traceback:
                done["traceback"] = frame.traceback
            self.emit(PROCESS, "step_out", **done)
            _TALLY.reset(tally_token)
            _STEP.reset(token)

    @contextmanager
    def item(self, number: int, of: int, **key: Any) -> Iterator["Step"]:
        """§4 -- one item of a step that iterates. Every record between
        `item_start` and `item_done` carries `item` and `of`."""
        token = _ITEM.set((number, of))
        #: §4 -- the item's key IS its identity, and it is what `failed_keys`
        #: and `skipped_keys` carry. The number is the fallback when the caller
        #: passed no key at all.
        identity = next((str(v) for v in key.values() if v not in (None, "")),
                        str(number))
        started = time.perf_counter()
        self.emit(PROCESS, "item_start", **key)
        frame = Step(_STEP.get())
        try:
            yield frame
        except Exception as exc:
            frame.status, frame.reason = "failed", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            done = {"status": frame.status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    **frame.outputs}
            if frame.reason:
                done["reason"] = frame.reason
            self.emit(PROCESS, "item_done", **done)
            #: Two tallies, counted once each: the step's, for `step_out`, and
            #: the run's, for `run_end`.
            step_tally = _TALLY.get()
            if step_tally is not None:
                step_tally.record(frame.status, identity)
            self.run_tally.record(frame.status, identity)
            _ITEM.reset(token)

    def _io(self, fields: Any) -> dict[str, Any]:
        """§8 -- terse carries no inputs or outputs at all; normal cuts at 240;
        debug is whole."""
        if self.mode == TERSE:
            return {}
        return shorten(dict(fields or {}), self.mode)


@dataclass
class Tally:
    """§4 -- what a run or step that iterated items ends with: the count by
    status, and the keys of the ones that did not succeed, so nobody has to
    scroll back through the items to find them."""

    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    failed_keys: list[str] = field(default_factory=list)
    skipped_keys: list[str] = field(default_factory=list)

    def record(self, status: str, key: str) -> None:
        self.total += 1
        if status == "failed":
            self.failed += 1
            self.failed_keys.append(key)
        elif status == "skipped":
            self.skipped += 1
            self.skipped_keys.append(key)
        else:
            self.ok += 1

    def counts(self) -> dict[str, Any]:
        """All six, whenever anything was iterated. The empty lists cost a
        reader nothing -- §1.1 does not render an empty value -- and a
        consumer can rely on the shape being the same every time."""
        return {"total": self.total, "ok": self.ok, "failed": self.failed,
                "skipped": self.skipped, "failed_keys": list(self.failed_keys),
                "skipped_keys": list(self.skipped_keys)}


@dataclass
class Step:
    name: str
    status: str = "ok"
    reason: str = ""
    traceback: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)

    def ok(self, **outputs: Any) -> None:
        self.status, self.outputs = "ok", {**self.outputs, **outputs}

    def failed(self, reason: str, **outputs: Any) -> None:
        self.status, self.reason = "failed", reason
        self.outputs = {**self.outputs, **outputs}

    def skipped(self, reason: str, **outputs: Any) -> None:
        self.status, self.reason = "skipped", reason
        self.outputs = {**self.outputs, **outputs}


# ------------------------------------------------------- §5 external calls
def _call_payload(logger: Logger, request: dict[str, Any]) -> Any:
    """§8 -- terse: names only. normal: strings cut at 240. debug: whole."""
    if logger.mode == TERSE:
        return keys_only(request)
    return shorten(request, logger.mode)


def _call_response(logger: Logger, kind: str, response: dict[str, Any]) -> dict[str, Any]:
    """§5 -- what MATTERS about what came back, not everything. A 178 KB body
    cut to 240 characters tells a reader nothing; its size and type tell them
    everything. A model's text is the result, and is shown."""
    keep = {k: v for k, v in response.items()
            if k in ("status", "ok") or k.endswith("_tokens") or k.endswith("_requests")
            or k in ("stop_reason", "tool_uses", "rows", "bytes", "content_type", "keys")}
    if logger.mode == TERSE:
        return keep
    for name in ("text", "body"):
        if response.get(name) is not None and not (kind in ("http", "service") and name == "body"
                                                   and logger.mode != DEBUG):
            keep[name] = shorten(str(response[name]), logger.mode)
    if logger.mode == DEBUG:
        for k, v in response.items():
            keep.setdefault(k, v)
    return keep


def outbound_call(logger: Logger, *, peer: str, kind: str, operation: str,
                  endpoint: str = "", method: str = "", status: Any = None,
                  ok: bool = True, attempt: int = 1, duration_ms: float = 0.0,
                  credential_name: str = "", request: dict[str, Any] | None = None,
                  response: dict[str, Any] | None = None, error: str = "") -> None:
    """§5 -- one record per attempt, on `audit`. `operation` is what the call
    did, in one name; it is present in every mode and never cut."""
    fields: dict[str, Any] = {
        "peer": peer, "kind": kind, "operation": operation,
        "endpoint": endpoint, "method": method, "status": status, "ok": ok,
        "attempt": attempt, "duration_ms": round(duration_ms, 1),
        "credential_name": credential_name,
        "request": _call_payload(logger, dict(request or {})),
        "response": _call_response(logger, kind, dict(response or {})),
    }
    if error:
        fields["error"] = error
    logger.emit(AUDIT, "outbound_call", **fields)


def inbound_request(logger: Logger, *, route: str, method: str, status: int,
                    ok: bool, duration_ms: float) -> None:
    logger.emit(AUDIT, "inbound_request", route=route, method=method, status=status,
                ok=ok, duration_ms=round(duration_ms, 1))


# ------------------------------------------------------- §1.2 other libraries
#: Done at import of the service's logging module, NOT in a startup hook -- a
#: web server prints its own lines before any hook runs.
_QUIETENED = False
_NOISY = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx",
          "httpcore", "anthropic", "mcp", "asyncio", "urllib3", "botocore")


class _AsSystem(logging.Handler):
    """A warning or error from a library is re-emitted as a `system` record."""

    def __init__(self, logger: Logger) -> None:
        super().__init__(level=logging.WARNING)
        self._logger = logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._logger.system("library_warning", library=record.name,
                                message=record.getMessage())
        except Exception:                                # noqa: BLE001
            pass


def quieten(logger: Logger) -> None:
    """Only this spec's records reach the console (§1.2)."""
    global _QUIETENED
    for name in _NOISY:
        lib = logging.getLogger(name)
        lib.setLevel(logging.WARNING)
        lib.handlers = [_AsSystem(logger)]
        lib.propagate = False
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers = [_AsSystem(logger)]
    _QUIETENED = True


# ------------------------------------------------------------ §6 + assembly
def configure_logging(service: str, *, folder: str | None = None,
                      mode: str | None = None, version: str = "0.1.0",
                      credentials: dict[str, str] | None = None,
                      dependencies: dict[str, Any] | None = None) -> Logger:
    """Everything in Part 1, set up by this one call (§10).

    `credentials` maps a credential's NAME to where it came from -- never its
    value (§6). `dependencies` maps a peer to ok/unreachable, the startup
    handshake with each peer the service cannot work without.
    """
    logger = Logger(Settings.read(service, folder, mode, version))
    quieten(logger)
    logger.system("service_start", version=version,
                  config=redact({"service": logger.service, "mode": logger.mode,
                                 "log_dir": str(logger.settings.log_dir or "(stdout)")})
                  if logger.mode != TERSE else {"keys": ["service", "mode", "log_dir"]})
    for name, source in (credentials or {}).items():
        logger.system("credential_resolved", credential_name=name, source=source)
    for peer, state in (dependencies or {}).items():
        ok = state is True or state == "ok"
        logger.system("dependency_check", peer=peer,
                      status="ok" if ok else "unreachable",
                      **({} if ok else {"reason": str(state)}))
    return logger
