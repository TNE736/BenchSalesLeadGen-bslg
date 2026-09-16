# CLAUDE.md — Bench Outreach Pipeline

Guidance for Claude working in this repository. Read before any change.

## 1. What this is

Phase 1 of the Bench Outreach Pipeline: load 300 bench consultants into HubSpot,
email each about the one requisition they are grouped under, classify replies,
research the interested ones, send a qualifying follow-up, and hand qualified
people to the Bench TA team. Phase 2 (the TA dashboard) is built only after
Phase 1 runs end to end.

The design source is `docs/DESIGN_SOURCE.md` (v0.4, 16 Sep 2026).

## 2. How we build — step by step, verified each time

**The design document is not fully verified.** Do not implement ahead of it and
do not implement the whole flow at once.

Before starting any step, stop and confirm with the user:

1. **What is the task** for this step.
2. **What are the inputs** — exact fields, sources, formats.
3. **What are the expected outputs** — exact HubSpot properties, files, responses.
4. **What has changed** since the document, as defined by the user at that moment.

Only after those four are agreed does code get written. Then: implement, test with
mocks, show the result, and get the step signed off before moving on. Record what
was agreed in `docs/STEPS.md` as each step closes.

Steps 1–6 in the document are agreed in principle; steps 7 onwards are still under
review and must not be built from the document alone.

## 3. Tech

Python 3.10+, no framework lock-in. FastAPI for the Gateway and Event Receiver
(HTTP ingress), plain typed modules elsewhere. pytest with every external service
mocked — the suite must run offline with no credentials.

`pip install -e .`; source lives under `src/bench_outreach/`.

## 4. Conventions

- **HubSpot is the system of record.** Agents hold no canonical lead state; on
  conflict HubSpot wins. `qualification_stage` is the progress field.
- **Stages:** loaded -> emailed -> engaged -> researched -> followed_up ->
  qualified -> handed_off. Closing stages: suppressed, closed, invalid, referred.
  Only `loaded` contacts start the flow.
- **No cross-module imports between agents** — shared code lives in
  `src/bench_outreach/common/`.
- **No fabricated engagement.** delivered / bounced / replied come only from
  Mailgun's signed webhooks. Opens and clicks are reports-only and never move
  anyone forward.
- **Nobody dropped silently.** Every unworkable record gets an explicit written
  reason (`bad data: ...`, `crm error: ...`) on the contact or in a failures list.
- **Webhook authenticity:** the Event Receiver verifies Mailgun's HMAC signature.
  `SKIP_MAILGUN_SIGNATURE` is local-dev only.
- **Secrets** come from `.env` (git-ignored) locally, from a secret manager in
  deployment. Never hard-coded, never pasted into chat, never committed.
- **Tools own their retry behaviour** — 3 tries, exponential backoff on 429/5xx.
  A tool failure never silently advances or drops a consultant.
- **Real consultant data never enters the repo.** `data/samples/` holds synthetic
  fixtures only.

## 5. Git

- Never push to a remote or open a PR without explicit confirmation from the user.
- Default branch: `dev`. Work on `step-<n>-<slug>` branches off `dev`.
- Commit messages: `step <n>: <imperative summary>`. One logical change per commit.
- Leave the tree committed locally, or explicitly dirty and noted — never
  mid-refactor with no record of intent.

## 6. Things to avoid

- Do not build steps 7+ from the document alone — they are under review.
- Do not copy code from LQABR. It is reference only; anything reused is a decision
  recorded in `docs/adr/` first.
- Do not add a second system of record.
- Do not scrape LinkedIn, or any source that forbids automated collection.
- Do not exceed the 50-emails-a-day cap or send from an unwarmed address.
- Do not disable signature verification outside local development.
- Do not add SMS or voice — this flow is email only.
