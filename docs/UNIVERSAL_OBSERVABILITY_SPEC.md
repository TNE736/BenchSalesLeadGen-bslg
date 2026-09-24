# Universal Observability Specification

**Version 2.1 · Universal**

Self-contained. Implementable in one pass, in any language. Every value is
exact: two implementations must produce byte-identical records for the same
input.

---

## What this produces

Three record streams per service — `process`, `audit`, `system` — as JSON, one
object per line. Every record carries one trace id, created where work enters
the service and carried through everything that work touches. Credentials never
appear. Telemetry never fails a run.

Part 10 carries the same trace id into OpenTelemetry traces and metrics.

**Not covered.** Do not infer a requirement for: log shipping, retention,
sampling, alerting, dashboards, personal data other than credentials, access
control, encryption. If one is needed, ask — do not design it in.

---

## The ground rule

Never close a gap by inference, by analogy, or by copying what existing code
does.

**Stop and ask** when: an input is missing; a case is not described here; two
parts appear to conflict; this document conflicts with existing code or another
instruction; a required capability is unavailable; or applying a requirement
would change behaviour outside observability.

**Never invent** a stream name, a field name, an event name, a limit, a default,
a redaction-list entry, or behaviour for an uncovered case.

**When stopped:** ask, implement everything not blocked, and say plainly what is
not done. Never ship a guess.

**No illusion.** Partial is *not met*. Not run is *not passed*. Unverified is
*not working*.

---

## The three inputs

Everything else in this document is fixed, or has a default.

### Input 1 — Where the application starts

The list of entry points per service: every place a unit of work begins.
Normally fewer than five.

```
an HTTP request arrives
a command-line invocation begins
background work begins after a response was already sent
a scheduled job fires
a message is consumed
```

These are the only places allowed to create a trace id (§5.2). An incomplete
list means trace ids get created in the wrong places, which is the failure
mode hardest to see afterwards.

**And every boundary the flow crosses without headers** — a third-party
system, a queue without header support, a scheduler, a manual step — with the
field that carries the id across it:

```
<boundary>: <carrier field>
```

The service before the boundary writes the trace id into that field; the
service after it adopts the id from that field (§5.8). This is what makes one
id span the whole flow instead of one service.

Where the flow starts — the first service in it — is the only place a trace id
is created in the normal case. Every later service adopts.

### Input 2 — Where the trace id is stored

The context-local mechanism for the language in use.

| Language | Mechanism |
|---|---|
| Python | `contextvars.ContextVar` |
| Node.js | `AsyncLocalStorage` |
| Java | `ScopedValue`, or MDC with explicit propagation |
| Go | `context.Context` |
| C# | `AsyncLocal<T>` |
| Ruby | `Fiber`-local storage |

It **MUST** follow the unit of work into async continuations and into
thread-pool work started within it, and **MUST NOT** be visible to other units
of work. A global, or a thread-local without async propagation, leaks one unit
of work's id into another.

### Input 3 — Where records are stored, and in what format

**Where** — a directory path, or the empty string for standard output only.
The three file names inside it are derived (Part 9) and are not configurable.

**Format** — exactly one of:

| Value | Meaning |
|---|---|
| `json` | Part 2. One object per line. The only format anything may parse. |
| `text` | Part 9.1. For a person reading a run directly. |
| `auto` | `text` when records go to standard output **and** standard output is an attached terminal; `json` in every other case. Files are therefore always `json` under `auto`. |

Default `auto`.

Nothing may parse a `text` record. Tooling, alerting and correlation read
`json`. If in doubt, choose `json`.

---

## 1. Configuration

Three values, supplied by the operator, all with defaults. They never block
implementation.

| Setting | Default | Empty / zero means |
|---|---|---|
| `destination` | `logs/<service>` | write no files; records go to standard output |
| `format` | `auto` | — |
| `mode` | `normal` | — |
| `max_bytes`, `backups` | `52428800`, `5` | `max_bytes = 0`: never rotate. `backups = 0`: truncate |

`mode` is exactly one of `terse`, `normal`, `debug`. `format` is exactly one of
`json`, `text`, `auto`. An unrecognised `format` resolves to `auto` and emits
`config_invalid`.

One module per service reads configuration; nothing else reads the environment.
Precedence: environment variable, then configuration file, then default.

An unrecognised `mode` resolves to `normal` and emits `mode_fallback`. A
negative or unparseable number resolves to its default and emits
`config_invalid`. Neither raises.

**Not configurable:** stream names, file names, field names, event names,
truncation limits, redaction lists. Fixed here so two deployments cannot
disagree.

---

## 2. The record

Required on every record. Field order is not significant.

| Field | Type | Exact form |
|---|---|---|
| `ts` | string | RFC 3339, UTC, three fractional digits, `Z`. `2026-09-21T04:34:34.512Z` |
| `stream` | string | `process` \| `audit` \| `system` |
| `service` | string | `^[a-z][a-z0-9_-]*$` |
| `service_version` | string | from the service's version constant, never a literal |
| `event` | string | `^[a-z][a-z0-9_]*$` |
| `trace_id` | string | 32 lowercase hex, or `""` |
| `severity` | string | `INFO` \| `WARN` \| `ERROR` |

`span_id` — 16 lowercase hex — is required from Level 2.

**Severity is outcome, not verbosity**, and the two are never derived from each
other:

| Condition | Severity |
|---|---|
| `step_end` with `status` = `failed` | `ERROR` |
| `step_end` with `status` = `skipped` | `WARN` |
| `outbound_call` / `inbound_request` with `ok` = `false` | `ERROR` |
| `sink_unavailable`, `sink_rotate_failed`, `mode_fallback`, `config_invalid`, `trace_missing` | `WARN` |
| everything else | `INFO` |

There is no `DEBUG` severity.

**Serialisation.** One JSON object per line, `\n` terminated. A value containing
a newline is escaped, never split. A value that cannot be serialised is replaced
by its native string representation, never dropped and never raised. The
literals this document defines — `<redacted>`, `... (+N chars)`,
`<depth-limited>` — are ASCII.

**Identifiers.** A record about one identifiable thing carries that thing's id
as an ordinary field. Use the name the application already uses; this document
does not rename application data.

---

## 3. The three streams

Exactly three. Never a fourth. Anything that fits neither `audit` nor `system`
goes on `process`.

| Stream | Contains | Answers |
|---|---|---|
| `process` | the work: steps, decisions, outcomes, counts | Where did this get stuck, and what did the service decide? |
| `audit` | every call that crossed this process boundary, in or out | Did this request happen, what did it ask for, what came back? |
| `system` | lifecycle, resolved configuration, telemetry's own failures | What is this process, and how was it configured? |

---

## 4. The events

Fixed names and fields. Emit all of them where the condition occurs; never
rename them. Additional events are allowed and follow Part 2.

### `process`

**`step_start`** — `step` (string, `^[a-z][a-z0-9_]*$`), plus the values the
step was handed.

**`step_end`** — emitted exactly once per `step_start`, on normal return, early
return, and exception.

| Field | Type |
|---|---|
| `step` | string, same as its `step_start` |
| `status` | `ok` \| `failed` \| `skipped` |
| `duration_ms` | number, at most one decimal |
| `reason` | string, required when `failed` or `skipped` |

On an exception, `status` is `failed`, `reason` is
`"<ExceptionTypeName>: <message>"`, and the exception is re-raised unchanged.

**`work_scheduled`** — emitted by the unit of work that schedules background
work, before its response is sent. No extra fields: it marks that work
continues under this trace id after the response. §5.6.

### `audit`

**`outbound_call`** — one call leaving this process, once per attempt.

| Field | Type |
|---|---|
| `peer` | string, the logical name of the far side |
| `endpoint` | string, URL or operation name |
| `method` | string |
| `status` | integer, or `null` if no response |
| `ok` | boolean |
| `attempt` | integer ≥ 1 |
| `duration_ms` | number |
| `credential_name` | string, the credential's **name**, `""` if none |
| `params` | object, per Part 7 |
| `error` | string, `""` when `ok` |

**`inbound_request`** — `route` (the route *template*, never the filled-in
path), `method`, `status`, `ok`, `duration_ms`.

### `system`

| Event | Fields |
|---|---|
| `service_start` | `config` (redacted object), `destination`, `mode` |
| `service_stop` | — |
| `sink_unavailable` | `path`, `reason` |
| `sink_rotate_failed` | `path`, `reason` |
| `mode_fallback` | `requested`, `resolved` |
| `config_invalid` | `setting`, `given`, `resolved` |
| `trace_missing` | `where` |

---

## 5. The trace id

### 5.1 Format

32 lowercase hex characters from 16 bytes of a cryptographically secure random
source. The all-zero value is invalid; if generated, generate again.

This is the W3C Trace Context shape, used whether or not OpenTelemetry is
present, so Part 10 adopts it with no change to any record.

### 5.2 Only entry points create it

Input 1 is the list. **No other code in the service may create a trace id.**

### 5.3 Adopt, or create

At an entry point, in this order, stopping at the first that applies:

1. A valid `traceparent` header is present → adopt its trace id.
2. This entry point is on the far side of a boundary listed in Input 1, and the
   carrier field holds a valid id — 32 lowercase hex, not all zeros → adopt it.
3. Otherwise → create one (§5.1).

```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             ^^ ^----------- trace id ----------^ ^--- parent ---^ ^^
```

Valid when **all** hold: it splits on `-` into exactly 4 parts; part 0 is `00`;
part 1 is 32 characters of `[0-9a-f]` and not all zeros; part 2 is 16 characters
of `[0-9a-f]` and not all zeros. Anything else is absent. Parsing never raises.

### 5.4 Set at entry, restore at exit

Set the store at the entry point; restore its previous value when the unit of
work ends, including when it ends by exception.

A store set once at process start and never cleared puts the first unit of
work's id on every later one.

### 5.5 Every record reads the store

The record writer reads it. No caller passes it in; no function signature
carries it.

If the store holds `""` when a record is written, the record carries
`trace_id: ""` and the service emits `trace_missing` once per unit of work,
naming where. A trace id is **never invented** at record-writing time.

### 5.6 Background work continues the trace id

Work that continues after the response was already sent keeps the trace id of
the unit of work that scheduled it.

The id **MUST** be captured when the work is scheduled and set in the store
when the work starts. Background work **MUST NOT** rely on inheriting the
context: by the time it runs, the scheduling scope may already have restored
the store (§5.4), and in some frameworks the copy is taken at the wrong moment.
Capture explicitly; set explicitly.

### 5.7 Every outbound call sends it

```
traceparent: 00-<trace id>-<16 lowercase hex>-01
```

The third field identifies this hop; without spans, generate 16 hex from 8
random bytes per call, never all zeros. If the store holds `""`, send no header.

### 5.8 Carrying the id across a boundary without headers

When work crosses something that does not propagate headers — a third-party
system, a queue without header support, a scheduler — the id travels **as
data**, in the carrier field named for that boundary in Input 1.

**The writer.** Every write that will trigger the next hop **MUST** include the
current trace id in the carrier field, alongside the data that triggers it. It
overwrites any earlier value: the record carries the id of the latest flow
that touched it.

**The reader.** The service on the far side adopts from the carrier field at
its entry point (§5.3, branch 2). If the field is absent or invalid, it creates
a new id and emits `trace_missing` with `where` naming the boundary — so a
broken carrier is visible, not silent.

**Fan-out.** One write that triggers many downstream units of work — one
record updated on many objects — puts all of them under one trace id. That is
the intent: the whole flow, from its first service to its last, under one id.
At Level 2 check the tracing backend's per-trace span limit before relying on
this for wide fan-outs.

The carrier field is application data. Its name must not contain a
`SECRET_SUBSTRING` (§6), or the id will be redacted from the record that
carries it.

---

## 6. Redaction

Applied to every field, at every nesting level, immediately before
serialisation. Lowercase the field name, then evaluate in this order, stopping
at the first match:

1. in **SAFE_EXACT** → keep
2. ends with an **IDENTIFIER_SUFFIX** → keep
3. contains a **SECRET_SUBSTRING** → replace with `<redacted>` if the value is
   non-empty, `""` if it is empty, absent or null
4. otherwise → keep

`ts`, `stream`, `service`, `service_version`, `event`, `trace_id`, `span_id`
and `severity` are never redacted.

**SAFE_EXACT** — full lowercased names, checked first:

```
max_tokens   input_tokens   output_tokens   total_tokens
prompt_tokens   completion_tokens   token_count   token_limit
auth_method   auth_type   auth_scheme   retry_count
```

**IDENTIFIER_SUFFIXES** — a name ending with one of these *names* a credential
rather than holding one:

```
_name   _source   _ref   _kind   _type
_scheme _method   _count _limit  _version
```

`_id` is deliberately absent: `session_id` holds a secret.

**SECRET_SUBSTRINGS**:

```
token   secret   password   passwd   pwd
api_key apikey   authorization        auth
bearer  credential          private_key   privatekey
access_key      accesskey   session_id    sessionid
cookie  signature           salt          passphrase
```

Objects and arrays are walked to a maximum depth of **6**; deeper values become
`<depth-limited>`.

**Redaction is unconditional.** No mode, configuration or caller weakens it.
`<redacted>` is a substitution, not a truncation, so nothing in Part 7 applies
to it.

A credential's **name** is required on `outbound_call` as `credential_name`.
Rules 1 and 2 exist so that field survives.

---

## 7. Modes

Mode affects only how large values are rendered. It never affects which records
are written, which fields are present, severity, or redaction.

| | `terse` | `normal` | `debug` |
|---|---|---|---|
| a `*_preview` field | omitted entirely | first **240** chars, truncated per below | full value |
| any other string | first **500** chars | first **500** chars | full value |
| a string inside `params` | replaced by the sorted key list under `keys` | first **60** chars | full value |
| `params` on `outbound_call` | `{}` | present | present, full |

**The truncation form**, exactly — ASCII only, no ellipsis character:

```
<first N characters><space>... (+<M> chars)
```

`M` is the number of characters removed. Retained characters are never
altered: whitespace, newlines and casing inside them are preserved exactly.

**In `debug`, a value is present whole or absent.** Never combined with a
description of itself — no length markers, no ellipses, no nested object
rendered as one string. Debug removes limits; it adds no fields, records or
streams.

Rendering to a terminal is the `text` format of §9.1, selected by Input 3. It
is normative, so two people reading the same run see the same thing.

---

## 8. Failure

Every failure inside the telemetry path is caught, recorded if possible, and
survived. No exception from the telemetry path reaches the caller.

| Failure | Behaviour |
|---|---|
| destination cannot be created or written | emit `sink_unavailable`, continue to standard output |
| rotation raises | emit `sink_rotate_failed`, keep appending to the current file |
| a value cannot be serialised | substitute its native string representation |
| the store holds `""` | emit `trace_missing`, write `trace_id: ""` |
| `mode` unrecognised | emit `mode_fallback`, use `normal` |

`sink_unavailable` and `sink_rotate_failed` are emitted **once per file per
process**, not once per record. A telemetry failure that reports on every line
is worse than one that reports once and continues.

---

## 9. Files

When `destination` is non-empty:

```
<destination>/<service>_process.log
<destination>/<service>_audit.log
<destination>/<service>_system.log
```

The live file always has exactly this name, whatever the format. A relative
`destination` resolves against the application root.

### 9.1 The `text` format

One record is one line, plus zero or more continuation lines.

```
HH:MM:SS <mark> <event padded to 24> <key>=<value> <key>=<value> ...
```

| Element | Exact form |
|---|---|
| time | local time from `ts`, 24-hour, zero-padded, no fractional part |
| mark | one ASCII character: `.` for `INFO`, `!` for `WARN`, `x` for `ERROR` |
| event | the `event` value, left-aligned, space-padded to 24 characters |
| fields | `key=value`, single space separated, in the order the record holds them |

`ts`, `stream`, `service`, `service_version`, `event` and `severity` are not
repeated as fields — they are the line's prefix or are implied by the file.
`trace_id` renders as `trace=` followed by its **first 8 characters**.

A value is rendered per the Part 7 mode rules, then any newline inside it is
replaced by a single space.

**No line may exceed the output width** — the attached terminal's width, or 120
when it is not known. Fields that do not fit continue on lines indented by
exactly 11 spaces. A line is never left to be wrapped by the terminal.

The 8-character `trace=` prefix makes a run scannable, not correlatable. Use
`json` to follow a trace.

### 9.2 Rotation Before writing, if `max_bytes > 0` and the live file is at least
`max_bytes`:

1. if `backups = 0`, truncate and stop
2. delete `<file>.<backups>` if it exists
3. for `k` from `backups-1` down to `1`, rename `<file>.k` to `<file>.k+1`
4. rename the live file to `<file>.1`
5. open a new live file with the original name

If any step raises, Part 8 applies.

When `destination` is empty, all three streams go to standard output,
distinguished by the `stream` field. `stream` is a field and not a directory
precisely so this substitution needs no other change.

---

## 10. OpenTelemetry

Parts 1–9 need no OpenTelemetry. This part adds traces and metrics.

**10.1 Install a tracer provider in every process, even with no exporter.**
With none installed, OpenTelemetry returns an invalid context whose trace id is
all zeros. Every record would carry `00000000000000000000000000000000`,
correlation would be silently dead, and no test would fail.

**10.2 Stop generating; start mirroring.** The entry point starts a span;
OpenTelemetry's propagator performs the adopt-or-create of §5.3 itself. The
store of Input 2 stops generating and holds the current span's trace id. Field
name, format and the read in §5.5 are unchanged.

**10.3 Add `span_id`** — 16 lowercase hex, on every record.

**10.4 One span per step.** The `step_start`/`step_end` boundary **is** the
span; never introduce a parallel step mechanism. Span names are
`<service>.<step>` and carry no identifiers.

**10.5 Background work is a child span**, parented to the span that scheduled
it, under the same trace id. A child span outliving its parent is legal in the
OpenTelemetry data model; the parent ending does not end the trace. Across a
§5.8 boundary, where no parent span id crosses, the far side starts a new root
span **within the adopted trace id**.

**10.6 Metrics.**

| Instrument | Type | Unit | Attributes |
|---|---|---|---|
| `<namespace>.work.items` | counter | `{item}` | `service`, `outcome` |
| `<namespace>.step.duration` | histogram | `ms` | `service`, `step`, `status` |
| `<namespace>.outbound.duration` | histogram | `ms` | `service`, `peer`, `endpoint`, `status_class` |

`status_class` is exactly one of `2xx`, `3xx`, `4xx`, `5xx`, `none`.

**10.7 The trace id reaches all three signals.**

| Signal | Where it lives |
|---|---|
| logs | the `trace_id` field (Part 2) |
| traces | it is the trace |
| metrics | an **exemplar** on the data point |

An exemplar is a field of the metric data point in the OpenTelemetry data
model, defined for this purpose. It carries the trace id and span id of the span
that recorded the measurement.

**10.8 Never a trace id in a metric's labels.** Not `trace_id`, `span_id`, an
application identifier, an address, a URL containing an id, a timestamp, or a
free-text reason.

Labels are the identity of a time series: each distinct combination creates a
new series, stored and billed for as long as retention lasts. One trace id label
is one series per trace. An exemplar attaches to an existing series and does not
change its identity, which is why it carries the same value safely.

Every new attribute needs a stated upper bound on its distinct values. "It is
usually small" is not a bound.

**10.9 If exemplars are unavailable**, the metrics-to-traces link is absent and
the join goes through `outbound_call`, which already carries the trace id and
the measurement. A label is never a substitute.

---

## 11. Conformance

| Level | Requires |
|---|---|
| **1 — Records** | Parts 1–9 |
| **2 — Traces** | Level 1 + §10.1–10.5 |
| **3 — Signals** | Level 2 + §10.6–10.9 |

A level is claimed only when every requirement in it is met. Partial is the
level below, plus a named gap.

### Acceptance tests

| # | Test |
|---|---|
| T1 | The three files exist at the exact paths of Part 9 after the first record on each stream. |
| T2 | With `format` = `json`, every line parses as JSON and carries all seven fields of Part 2 with the stated types. |
| T2b | With `format` = `text`, no line exceeds the output width; a value too long to fit continues on lines indented by exactly 11 spaces; and one record is still one logical line. |
| T2c | With `format` = `auto` and a destination set, the files contain JSON. |
| T3 | A `process` record is in the process file and in neither other file. |
| T4 | An `outbound_call` is in the audit file only. |
| T5 | `service_start` is on `system`, and its `config` contains no credential value. |
| T6 | Two requests with different `traceparent` headers produce two different trace ids, and neither appears on the other's records. |
| T7 | A request with no `traceparent` produces 32 lowercase hex, not all zeros. |
| T8 | A malformed `traceparent` produces a new valid trace id and raises nothing. |
| T9 | After a request completes, the store holds the value it held before it began. |
| T10 | Background work carries the **same** trace id as the request that scheduled it, including when it runs after the response was sent and after the request's store was restored; the request emitted `work_scheduled` before responding. |
| T10b | A service on the far side of an Input 1 boundary, given a record whose carrier field holds a valid id, adopts that id; given a record with the field absent, it creates a new id and emits `trace_missing` naming the boundary. |
| T10c | The write that triggers a boundary crossing includes the current trace id in the carrier field. |
| T11 | An outbound call sends a `traceparent` whose trace id equals the current record's, with a third field of 16 hex, not all zeros. |
| T12 | A step that returns, one that returns early, and one that raises each produce exactly one `step_start` and one `step_end`; the raising one is `failed`, has a non-empty `reason`, severity `ERROR`, and the exception still propagates. |
| T13 | `api_key` with a non-empty value renders as exactly `<redacted>`, at the top level and nested five deep. |
| T14 | `max_tokens` with value 2000 renders as 2000. |
| T15 | `credential_name` renders its value unchanged. |
| T16 | T13 still passes in `debug`. |
| T17 | In `normal`, a 1589-character `*_preview` value renders as 240 characters followed by exactly `... (+1349 chars)`. |
| T18 | In `debug`, the same field renders 1589 characters with no added text, still on one line. |
| T19 | With an unwritable destination the service starts, emits `sink_unavailable` once, writes to standard output, and completes a unit of work. |
| T20 | With a small `max_bytes`, rotation produces `<file>.1` and the live file keeps its exact name; with rotation forced to raise, `sink_rotate_failed` is emitted once and records keep being written. |
| T21 | *(L2)* Every record carries a 16-hex `span_id` equal to the span active when it was written. |
| T22 | *(L3)* No metric carries a trace id, span id or application identifier as an attribute. |

---

## 12. Implementation order

One pass. Each step is testable before the next begins.

| # | Do | Tests |
|---|---|---|
| 0 | Confirm Inputs 1, 2 and 3 are supplied | — |
| 1 | Configuration reader — Part 1 | defaults resolve; bad `mode` and bad `format` fall back |
| 2 | Record writer and the three sinks — Parts 2, 3, 9 | T1–T4, T2b, T2c |
| 3 | Redaction — Part 6 | T13–T15 |
| 4 | Modes — Part 7 | T17, T18 |
| 5 | Failure — Part 8 | T19, T20 |
| 6 | Trace store and entry points — §5.1–5.5 | T6–T9 |
| 7 | Outbound propagation — §5.7 | T11 |
| 8 | Background work, and boundary carriers — §5.6, §5.8 | T10, T10b, T10c |
| 9 | Step frames and events — Part 4 | T5, T12, T16 |
| 10 | OpenTelemetry — Part 10 *(Level 2+)* | T21, T22 |

Steps 1–9 are Level 1 and need no dependency beyond a JSON encoder and the
Input 2 mechanism.

---

## Change log

**2.1 Universal** — One trace id for the whole flow, not one per service. The
first service in the flow creates it; every later service adopts. Across a
boundary that carries no headers the id travels as data in a named carrier
field (Input 1, §5.8), and §5.3 gains a third branch to adopt from it.
**Reversal:** §5.6 no longer gives background work a new trace id — it
continues the scheduling unit's id, captured explicitly. The earlier rule
rested on a span-tree heuristic that does not apply to a log correlation id.
`work_started` / `triggered_by` replaced by `work_scheduled`; §10.5 restated
for child spans outliving their parent.

**2.0 Universal** — Inputs cut from seven to three: where the application
starts, where the trace id is stored, and where records are stored and in what
format. Everything else either has a default, is visible in the code, or
belongs to Level 2+. The `text` format is now specified exactly (§9.1) rather
than left as a non-normative console rendering, because it became an input.
Business-key naming removed as a requirement — the application keeps its own
names. Binding worksheet, question register and conformance-survey guidance
removed as governance rather than specification.

**1.1** — Added the ground rule, the no-illusion rule, and the question
register.

**1.0** — First self-contained, application-agnostic release: exact field
names and types, event vocabulary, redaction lists, truncation form, rotation
algorithm, `traceparent` validation, acceptance tests.
