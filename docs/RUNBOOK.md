# Runbook — how to run the flow

The day-to-day command card. Open this, work down it, done.

For *why* the ports are what they are and the history of what went wrong, see
`REAL_RUN.md`. This file is only the commands.

**Repo:** `C:\Users\StephenMiller\bench-outreach`
**Everything runs on Windows PowerShell.** Nothing in WSL — WSL has its own
`localhost`, so a tunnel started there cannot see a service started here.

---

## Ports — fixed, do not improvise

| Port | What | Reached by |
|---|---|---|
| 8080 | Agent Gateway | ngrok, from the internet |
| 8081 | Email Agent | the gateway only, on 127.0.0.1 |
| 4040 | ngrok inspector | you, in a browser |

---

## The order matters

**ngrok first, gateway second.** The gateway reads the public URL at startup, so
starting it before ngrok means it boots with the wrong address and every HubSpot
signature check fails.

---

## Step 0 — Pre-flight: what mode are we in?

Run this before anything else. It tells you whether real emails will be sent.

```powershell
cd C:\Users\StephenMiller\bench-outreach
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
python -c "from bench_outreach.email_agent.agent import redirect_target; from bench_outreach.common.settings import env_bool; t=redirect_target(); print('SEND     :', 'LIVE - Mailgun really sends' if env_bool('EMAIL_AGENT_SEND') else 'dry run - writes to a file'); print('REDIRECT :', (t+' - lead gets nothing') if t else 'OFF - emails go to the REAL LEAD')"
```

Three possible answers:

| Output | Meaning |
|---|---|
| `SEND: dry run` | Nothing is emailed. Drafts go to `logs\email_agent\drafts-<date>.md`. Safe. |
| `SEND: LIVE` + `REDIRECT: <address>` | Real Mailgun send, but every email lands with you. Safe. |
| `SEND: LIVE` + `REDIRECT: OFF` | **Real emails reach real consultants. Cannot be undone.** |

Both switches live in `.env`: `EMAIL_AGENT_SEND` and `EMAIL_AGENT_REDIRECT_TO`.

---

## Step 1 — Start the tunnel

**Terminal C**

```powershell
C:\ngrok\ngrok.exe http 8080
```

HubSpot lives on the internet and cannot reach this laptop. ngrok opens a public
HTTPS address that forwards to port 8080 here. Leave this window open — closing it
kills the tunnel.

Copy the `https://xxxx.ngrok-free.app` line it prints. You need it twice.

---

## Step 2 — Put that URL in `.env`

Open `.env` and set:

```
GATEWAY_PUBLIC_URL=https://xxxx.ngrok-free.app
```

**Why:** HubSpot signs the URL it posted to. The gateway recomputes that signature
using the URL it believes it has. One character different and every webhook is
rejected as forged.

**A free ngrok tunnel gets a new address every restart.** This step is not
one-time setup — it is part of every run.

---

## Step 3 — Start the gateway

**Terminal A**

```powershell
cd C:\Users\StephenMiller\bench-outreach
.\.venv\Scripts\Activate.ps1
cd src
uvicorn bench_outreach.gateway.app:app --port 8080
```

Receives HubSpot's webhook, verifies the signature, drops duplicates, decides the
route, hands the contact id to the email agent.

---

## Step 4 — Start the email agent

**Terminal B**

```powershell
cd C:\Users\StephenMiller\bench-outreach
.\.venv\Scripts\Activate.ps1
cd src
uvicorn bench_outreach.email_agent.app:app --port 8081
```

Does the work: reads the lead over HubSpot MCP, checks the guards, claims it,
Sonnet writes the email, Mailgun sends it.

---

## Step 5 — Check both are healthy

**Terminal D**

```powershell
curl.exe -s http://127.0.0.1:8080/readyz
curl.exe -s http://127.0.0.1:8081/readyz
```

Use `curl.exe`, not `curl` — PowerShell aliases bare `curl` to something else and
you get an ngrok interstitial page instead of an answer.

Read `"dry_run"` in the email agent's reply. It must match what Step 0 told you. If
it disagrees, a terminal is running older settings — restart it.

---

## Step 6 — Point HubSpot at the new URL

HubSpot → Development → Legacy Apps → `Agent-Gateway-Bench` → Webhooks

- **Target URL:** `https://xxxx.ngrok-free.app/hubspot/events`
- Subscription: object **Contact**, event **Property change**, property
  **`decision_maker`**, state **active**

**Every run, because the ngrok address changed.** A stale URL fails silently —
HubSpot gets an error from a dead tunnel and nothing appears in any of your
terminals. If Terminal A shows nothing at all, suspect this first.

---

## Step 7 — Fire it

In HubSpot, pick a contact where **Decision Maker is No or blank** and set it to
**Yes**. Save.

The webhook fires on *change*. A contact already set to Yes produces nothing.

Other reasons a contact is skipped, all correct behaviour:

- no email address on the record
- `email_status` is already SENT, DELIVERED, OPENED or UNSUBSCRIBED
- `hs_email_optout` is true
- `technology` matches nothing in `skills\outreach\assets\<technology>.json`

---

## What you should see, in order

**Terminal A — gateway**

```
signature verified - this is genuinely from HubSpot
contact 551358867185  decision_maker="true"  -> routed
agent accepted it - HTTP 200 in 20.5s (attempt 1)
1 received - 1 routed - 0 ignored - 1 handed off
```

**Terminal B — email agent**, five narrated steps ending in

```json
{"event":"run_finished","status":"sent","dry_run":false,
 "reference":"Salesforce Lead Developer","subject":"Salesforce Lead Developer requirement",
 "message_id":"<20260919100810.ff81a1039d2d8a4d@reply.tekninjas.com>"}
```

**HubSpot** — the contact's `email_status` becomes **SENT**.

`email_status` will never move past SENT. Delivered, opened and bounced need
Mailgun webhooks, which are deliberately not registered yet.

**The email** — in the recipient's inbox, usually within a few seconds. In dry run
it goes to `logs\email_agent\drafts-<date>.md` instead.

---

## Step 8 — Read the logs

The terminal shows the boxed story of each run and then scrolls away. Everything
else — every outbound call, its cost, which file versions wrote the email — is in
`logs\agents\`, as records written to the **Universal Observability Specification
v2.1** (`docs\UNIVERSAL_OBSERVABILITY_SPEC.md`). One command reads them:

```powershell
python scripts\logs.py --full               # EVERYTHING: timeline + prompt + email
python scripts\logs.py --trace              # one lead end to end, both services
python scripts\logs.py                      # the last run, in full
python scripts\logs.py --runs               # every run, one line each
python scripts\logs.py --run bslg-20260921T1103  # one run (a prefix is enough)
python scripts\logs.py --lead 551358867185  # every run for one contact
python scripts\logs.py --system             # SYSTEM only: startup, config, loaded
python scripts\logs.py --process            # PROCESS only: decisions per lead
python scripts\logs.py --audit              # AUDIT only: calls and what they cost
python scripts\logs.py --failures           # runs that did not send
python scripts\logs.py --gateway            # the gateway log instead
python scripts\logs.py --raw                # the JSON itself
```

`--trace` is the one to reach for after a real run. Since the specification
landed there is **one trace id for the whole flow**: the gateway creates it when
HubSpot's webhook arrives, writes it onto the contact in HubSpot, sends it to the
email agent in a `traceparent` header, and the email agent adopts it. Every line
of both services carries the same 32-hex string. `--trace` also still joins on
the trigger id, which is what an older run has.

### Where the log files are

One folder, three files per service, named by the specification (Part 9):

```
logs\agents\   gateway_process.log      gateway_audit.log      gateway_system.log
               email_agent_process.log  email_agent_audit.log  email_agent_system.log

artifacts\email_agent\      drafts-2026-09-21.md
artifacts\email_agent\runs\ <trace id>.md
artifacts\gateway\          outbox.jsonl
```

Where they go and in what format is the operator's choice (Input 3), set in each
service's yaml under `observability:` — today `destination: logs/agents`,
`format: json` — and overridable with `BENCH_LOG_DESTINATION`, `BENCH_LOG_FORMAT`,
`BENCH_LOG_MODE`, `BENCH_LOG_MAX_BYTES`, `BENCH_LOG_BACKUPS`. Everything else —
stream names, file names, field names, event names, limits, redaction — is fixed
by the specification and is not configurable.

**`logs\` holds records and nothing else.** Records are machine-readable, rotate at
50 MB with five backups, and carry no personal data. Drafts and run transcripts are
products of a run, are meant to be read by a person, and **do** carry a lead's name
and address — so they live under `artifacts\`, which is gitignored for that reason.

The gateway's `outbox.jsonl` is a decision parked because no agent URL was
configured — work waiting to be done. Not a log record.

### The record

Every line carries the same seven fields (Part 2), whatever else it carries:

```json
{"ts": "2026-09-21T16:20:30.536Z", "stream": "audit", "service": "gateway",
 "service_version": "0.1.0", "event": "outbound_call",
 "trace_id": "3e631730c0b1d11fff6dde13d06f9329", "severity": "INFO", ...}
```

`severity` is outcome, never verbosity: a `step_end` that failed is `ERROR`, one
that was skipped is `WARN`, a call that came back `ok: false` is `ERROR`, and
telemetry's own troubles (`sink_unavailable`, `trace_missing`, …) are `WARN`.
There is no DEBUG.

### The two ids

**`trace_id`** — 32 lowercase hex, random, created **once** per flow at the
gateway's `POST /hubspot/events` and adopted everywhere after. It travels two
ways: in the `traceparent` header on every call that leaves a process, and as
data in the `trace_id` property on the HubSpot contact, because HubSpot carries
no headers back to us. **Every campaign gets a new one**: `decision_maker` set to
Yes (`attemptNumber` 0) creates an id and overwrites the contact's old one. A
HubSpot **retry** (`attemptNumber` > 0) finds the original's id on the contact and
joins that flow instead of starting a second one. The email agent reads the id
back with the contact and checks it against the header; agree, mismatch or
absent, each is named in the log.

**`trigger_id`** — `bslg-<utc stamp>-<12 hex>`, **deterministic** per HubSpot
event, minted by the gateway's router. It is not a trace id and is never 32 hex,
so no parser can mistake one for the other. It answers *which HubSpot event
asked for this work* and is the same string on every redelivery of that event.

`run_id` is gone.

### The three kinds of log

They are labelled **in the terminal as it runs**, not just in the log file — the
second column on every line is the stream:

```
08:04:29 ┌─ Gateway → Email Agent   lead 551358867185   run trg-5868494a63e1
08:04:29 │  unmarked = process   ⟶ AUDIT = a call that left us   ⟶ SYSTEM = the service
08:04:29 │ STEP 1  Read contact 551358867185 from HubSpot via MCP
08:04:38 │    ✓ got 8 of 11 properties in 8.8s
08:04:38 │ ⟶ AUDIT   hubspot get_crm_objects  200  8.8s
08:04:38 │
08:04:38 │ STEP 2  Check the guards before writing anything
08:04:38 │    ✓ has an email address, not contacted before, not opted out
```

**Only the exception is marked.** `process` is the rule — nine lines in ten — so
tagging every one of them was noise in a different position. The header declares
the convention once; `AUDIT` and `SYSTEM` stand out because they are rare, which
is exactly what makes them worth seeing. The JSON still carries `stream` on every
single record, so `--system`, `--process` and `--audit` are unaffected.

Outbound calls used to be written to the file but never printed, so the terminal
showed a silent gap where HubSpot, Anthropic and Mailgun actually were. In the run
above that gap was 8.8 seconds long.


Every record carries exactly one `stream`, and the viewer prints it in its own
column so you always know which kind you are reading.

| stream | answers | holds | written when |
|---|---|---|---|
| `system` | is this service **set up right**? | startup, config, which skill files loaded, why it refused to start | at boot — true before any lead exists |
| `process` | what did we **decide**, and why? | the numbered steps, the guards and the values they judged, the skill invoked, the draft | once per lead |
| `audit` | what did it **cost**? | endpoint, status, duration, tokens, message id | once per call that left the process |

The rule that keeps them apart: **audit records what a call COST, process records
what it PRODUCED, system records what the service IS.** So token counts are audit,
the email's subject is process, and the loaded skill's sha is system.

Both services write all three, in the same shape, so one reader makes sense of
`gateway.jsonl` and `email_agent.jsonl` alike.

So `--audit` on its own is the bill:

```
10:07:57  trg-5a748b8752c0  hubspot    get_crm_objects       200    7.4s
10:08:00  trg-5a748b8752c0  hubspot    manage_crm_objects    200    3.7s
10:08:08  trg-5a748b8752c0  anthropic  messages.create       200    8.0s   3407 in / 159 out
10:08:10  trg-5a748b8752c0  mailgun    messages              200      -
```

And `skill_selected` in the process stream pins the exact material behind an
email — `skill_sha`, `asset_sha`, `reference_sha`, `prompt_sha`. Given any sent
email you can prove which four file versions produced it.

### Where the actual words are

The `.jsonl` deliberately holds no prose — only shas and counts — because a lead's
name and address must not sit in a line that gets grepped or pasted. But a sha
cannot answer *"why did this person get these words?"*.

So every run also writes `artifacts\email_agent\runs\<trace_id>.md`, containing three
sections: the **prompt sent to the model**, its **raw reply**, and the **email as
sent** with the footer. `--full` prints them under the timeline. The JSONL records
only the path, in `prompt_file`, `reply_file` and `sent_file`.

Those files contain personal data. They stay on this machine, `logs/` is
gitignored, and `BENCH_RUN_TRANSCRIPTS=false` in `.env` turns them off if that
ever stops being acceptable — then only the shas remain.

### Every event the email agent writes

| event | stream | what it pins down |
|---|---|---|
| `skill_loaded` | system | which `EMAIL_AGENT.md` and which skills were loaded at startup, with shas |
| `prompt_built` | process | the `EMAIL_AGENT.md` sha and skill shas behind this lead's prompt |
| `startup` (gateway) | system | ingress path, configured routes, any config problems |
| `run_started` / `run_finished` | process | the lead, the outcome, the duration, the message id |
| `step` | process | which of the five steps we are in |
| `outbound_call` | audit | one per call to HubSpot, Anthropic or Mailgun: status, duration, tokens |
| `contact_read` | process | which properties HubSpot returned and which were empty |
| `guards_passed` | audit | the actual value each guard judged, and whether it was switched on |
| `guard_stopped` | audit | which guard stopped it, and the value that did it |
| `hubspot_write_skipped` | audit | a claim we chose not to write, and why |
| `status_written` | audit | the value written back to `email_status` |
| `skill_selected` | process | technology, title, description, involves, and four shas: skill, asset, reference, prompt |
| `model_reply` | process | model, reply length, reply sha, transcript path |
| `draft` | process | subject in full, body length and sha |
| `footer_added` | process | postal address and unsubscribe token appended by code |
| `redirect_active` | process | the test redirect was on, so no lead was contacted |

Personal data never reaches the `.jsonl`. Addresses and phone numbers are blanked
on the way in; identifiers like `message_id` are kept deliberately, because that
is the key that will join Mailgun's delivery events back to the run that sent
the mail.

---

## Testing without ngrok or HubSpot

One contact, straight to the email agent. No tunnel, no gateway, no webhook.

```powershell
cd C:\Users\StephenMiller\bench-outreach
.\.venv\Scripts\Activate.ps1
python scripts\dry_run.py 551358867185
```

This is the fastest way to see what the model writes after changing the skill.
**It obeys `EMAIL_AGENT_SEND`** — if that is true, this really sends. The name is
historical.

Send a signed fake webhook to the local gateway, skipping HubSpot and ngrok:

```powershell
python scripts\fake_hubspot.py --object-id 551358867185
```

---

## If something doesn't appear

| Symptom | Cause |
|---|---|
| Nothing in Terminal A | HubSpot target URL is stale, or ngrok died. Check Step 6. |
| `signature_verified: false` | `GATEWAY_PUBLIC_URL` does not match the live ngrok URL. Step 2, then restart Terminal A. |
| Gateway logs a handoff, Terminal B silent | Email agent is not running, or not on 8081. |
| `dry_run: true` when you expected false | `.env` changed after that terminal started. Restart Terminal B. |
| `no assets/*.json entry for technology` | The contact's technology has no file in `skills\outreach\assets\`. The lead is flagged FAILED, never sent something generic. |
| Email agent says `skipped` | One of the guards. The reason is in the log line, and it is almost always correct. |
| Port 8080 already in use | Something in WSL or Docker is holding it. See `REAL_RUN.md`. |
| `curl` returns an ngrok HTML page | You used `curl` instead of `curl.exe`. |

---

## Shutting down

`Ctrl+C` in each terminal. Order does not matter.

Nothing persists between runs except the log files and whatever was written to
HubSpot — and a lead marked SENT stays marked. That is the point, but it also means
a test on a real contact takes them out of the campaign permanently unless you
clear `email_status` by hand.

---

## The two switches, once more

```
EMAIL_AGENT_SEND=true                            # false = nothing is emailed
EMAIL_AGENT_REDIRECT_TO=stephen.m@tekninjas.com  # commented out = real leads
```

While the redirect is on, the lead is **not** marked SENT — they received nothing,
so they stay contactable. Commenting that line out is the last thing you do before
going live, and Step 0 is how you check which way it is set.
