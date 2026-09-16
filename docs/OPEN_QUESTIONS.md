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
| 6 | **HubSpot plan.** Requisition records use custom objects, which normally need Enterprise. | — | Step 1 and step 2 | _open_ |
| 7 | **Two points from step 6.** Treat "marked as spam" like an unsubscribe; use the sender's address as the third way to match a reply. | Yes to both | Step 6 | _open_ |
| 8 | **Results split by requisition** on the dashboard. | — | Phase 2 | _open_ |
