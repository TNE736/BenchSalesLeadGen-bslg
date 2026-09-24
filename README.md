# Bench Outreach Pipeline

Email bench consultants about an open role, research the ones who reply, and hand
only genuinely qualified people to the Bench TA team.

**Source design:** `docs/DESIGN_SOURCE.md` (Bench Outreach Pipeline, design doc v0.4,
16 Sep 2026) — a **flow reference only**, not a spec. Each step is defined fresh at
build time; standing decisions live at the top of `docs/STEPS.md`.

**This is a new repository. LQABR is reference only** — nothing is copied from it
without a decision recorded here first.

## Status

| Step | What it is | State |
|---|---|---|
| 0 | Repo skeleton | **done** |
| 1 | Inputs: consultant list + requisitions in HubSpot | **done by hand** — new HubSpot account 247408852, contacts imported; see docs/STEPS.md |
| 2 | Loader → HubSpot contacts | first load done by import; no requisition linking (dropped) |
| 3 | Agent Gateway: `decision_maker = true` → webhook → gateway → hand-off | **built** — HubSpot webhook subscription still to be pointed at it |
| 4 | HubSpot access via HubSpot's hosted MCP (`mcp.hubspot.com`) | **done** — consent run, verified live |
| 5 | Email Agent — Claude works each lead with tools: reads the outreach skill, writes, sends | **built** — live sends verified; tool-loop redesign 19 Sep 2026 |

Each step is agreed before it is built: task, inputs, expected outputs — verified
against the design doc plus whatever has changed since.

## Layout

```
skills/<name>/            SKILL.md + assets/ + references/ — read by the model on demand
src/bench_outreach/
  email_agent/           receives the hand-off; the model reads the skill, writes, sends via tools
  common/settings.py     .env loading
  common/hubspot_mcp.py  the ONLY HubSpot access: MCP client for mcp.hubspot.com
  gateway/               Agent Gateway — signature, events, router, dispatch, app
config/gateway.yaml      routes: which property + value goes to which agent
scripts/hubspot_auth.py  one-time HubSpot MCP consent (then agents run headless)
scripts/fake_hubspot.py  fire a signed webhook at a local gateway
docs/              design source (flow reference), as-built step log, open questions
tests/unit/        pytest — offline, no credentials
```

Packages for later steps are created when their step is agreed, not before.

## Getting started

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -r requirements-dev.txt
pip install -e .
cp .env.example .env        # fill in locally; never commit
pytest

# run the gateway
cd src && uvicorn bench_outreach.gateway.app:app --port 8080
# in another shell: send a signed test webhook
python scripts/fake_hubspot.py --url http://localhost:8080
```

## Non-negotiables

- HubSpot is the only system of record. No agent keeps lead state.
- Agents reach HubSpot only through HubSpot's hosted MCP (`common/hubspot_mcp.py`).
- Engagement is never fabricated — delivered/bounced/replied come only from
  Mailgun's signed notices.
- Nobody is dropped silently: every unworkable consultant gets a written reason.
- Every email carries a postal address and a working unsubscribe link (CAN-SPAM).
- Opt-outs are global and permanent, honoured within one send cycle.
- LinkedIn is never scraped.
- Max 50 emails a day from the warmed-up sending address.
