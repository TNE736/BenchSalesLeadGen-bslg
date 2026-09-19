# Open questions

From section 11 of the design doc, plus anything raised during the build. Each one
blocks something specific. Answers get recorded here with a date, and the affected
step's entry in `docs/STEPS.md` is updated.

| # | Question | Doc's recommendation | Blocks | Answer |
|---|---|---|---|---|
| 1 | **Research source.** LinkedIn cannot be scraped. Licensed enrichment API, or public web pages only (cheaper, finds noticeably less). Carries a cost. | — | Research Agent (step 7+) | _open_ |
| 2 | **Recruiter call.** Does the agent book the call, or stop at handoff? | Stop at handoff for Phase 1 | Handoff step | _open_ |
| 3 | **Reminder email.** Is there one for people who never reply; how long we wait; how many we send. | One reminder, after four working days | Reminder step | _open_ |
| 4 | **Sender details.** Assistant name, company name, postal address, exact sending address. | — | First real send (step 5) | _open_ |
| 5 | **Email #1 wording.** Final wording of the template. | — | First real send (steps 4–5) | _open_ |
| 6 | **HubSpot plan.** Requisition records use custom objects, which normally need Enterprise. | — | — | **closed 16 Sep** — no requisition records; dedicated account 247408852 in use |
| 7 | **Two points from step 6.** Treat "marked as spam" like an unsubscribe; use the sender's address as the third way to match a reply. | Yes to both | Step 6 | _open_ |
| 8 | **Results split by requisition** on the dashboard. | — | Phase 2 | _open_ |

---

## Deferred: one `trace_id` per lead, carried by HubSpot

**Raised 17 Sep 2026 from the LQABR "Trace ID Approval Brief". Agreed in principle,
deferred — not scheduled.**

**The idea.** One `trace_id`, created once at the start of a lead's journey and
stored **on the HubSpot contact**. Every agent reads it off the record that woke it
and writes it back beside whatever it changes. Every log line everywhere carries it,
so one search returns a lead's whole journey.

The carrier is the clever part: the id lives on the record, not in the hand-off
message, so it survives a restart, a scale-to-zero, a Mailgun webhook arriving days
later, and a lead re-entering the flow.

**Why we need it too.** Our `trigger_id` is a hash of the HubSpot *event*, not the
lead's journey. It holds across one cycle (gateway → email agent → Mailgun variable)
but a later event — a reply, a research hop — mints a new one and the chain breaks.
The moment step 6 exists we have LQABR's problem with two ids instead of seven.

**Cheap for us, and cheaper now than later:** two agents rather than five,
`trigger_id` already plumbed end to end, one HubSpot surface (`common/hubspot_mcp.py`),
and nothing live to backfill. Roughly 150 lines.

**What it would touch**
1. New HubSpot contact property `trace_id` (text), created via `manage_custom_properties`.
2. Email agent: on first read, mint one if absent and write it with the `email_status` claim.
3. `common/trace.py`: carry `trace_id` on every record.
4. Mailgun message: `bo_trace_id` alongside `bo_trigger_id`.
5. Step 6's event receiver: read it back off the Mailgun variable.

**The design decision to settle first — does the gateway read HubSpot?**
Today it never does: it takes an id, decides, hands off. No CRM dependency, no
latency, no lead data. Stamping `trace_id` on its own log lines means reading the
contact first, which buys complete coverage at the cost of a HubSpot call before
every dispatch and a new failure mode (HubSpot down → cannot route).
**Recommendation: don't.** Let the email agent log both ids on one line
(`trigger trg-… → trace lqt-…`); that single line joins the two searches.

**Still open**
- Who mints it here? LQABR's summary agent starts the journey; we have none, so the
  email agent would mint on first touch.
- Same person, second campaign — same `trace_id` or a new one? "Once per lead" gives
  one person's whole history but loses per-campaign separation.
- Format: the brief shows `lqt-<ULID>` — prefix plus a time-sortable id. Ours needs
  its own prefix and the same sortability.
