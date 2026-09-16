# Bench Outreach Pipeline

Email bench consultants about an open role, research the ones who reply, and hand
only genuinely qualified people to the Bench TA team.

**Source design:** `docs/DESIGN_SOURCE.md` (Bench Outreach Pipeline, design doc v0.4,
16 Sep 2026). Steps 1–6 are agreed; everything after is still under review.

**This is a new repository. LQABR is reference only** — nothing is copied from it
without a decision recorded here first.

## Status

| Step | What it is | State |
|---|---|---|
| 0 | Repo skeleton | **done** |
| 1 | Inputs: 300-row consultant sheet + 5 requisitions in HubSpot | not started |
| 2 | Loader → HubSpot contacts, linked to requisitions | not started |
| 3 | HubSpot → Gateway trigger, stage routing | not started |
| 4 | Email Agent writes Email #1 | not started |
| 5 | Email #1 sent through Mailgun | not started |
| 6 | Mailgun notices + replies → Event Receiver → Gateway | not started |
| 7+ | Reply meaning, reminder, research, Email #2, qualification, handoff | **design under review** |

Each step is agreed before it is built: task, inputs, expected outputs — verified
against the design doc plus whatever has changed since.

## Layout

```
src/bench_outreach/
  common/          shared types, stages, config, HubSpot/Mailgun clients
  loader/          step 2 — Excel sheet -> HubSpot contacts
  gateway/         step 3 — trigger ingress, decides which agent acts next
  email_agent/     steps 4-5 — builds and sends Email #1 (later Email #2)
  event_receiver/  step 6 — Mailgun notices and replies, signature-verified
  research_agent/  step 7+ — matched skills and gaps (source still open)
docs/              design source, step plans, open questions, ADRs
tests/unit/        pytest, all external services mocked
data/samples/      small synthetic fixtures only — never real consultant data
config/            non-secret config
scripts/           one-off operator scripts
```

## Getting started

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements-dev.txt
pip install -e .
cp .env.example .env        # fill in locally; never commit
pytest
```

## Non-negotiables

- HubSpot is the only system of record. No agent keeps lead state.
- Engagement is never fabricated — delivered/bounced/replied come only from
  Mailgun's signed notices.
- Nobody is dropped silently: every unworkable consultant gets a written reason.
- Every email carries a postal address and a working unsubscribe link (CAN-SPAM).
- Opt-outs are global and permanent, honoured within one send cycle.
- LinkedIn is never scraped.
- Max 50 emails a day from the warmed-up sending address.
