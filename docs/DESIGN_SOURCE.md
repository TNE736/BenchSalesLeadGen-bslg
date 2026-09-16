# Design source

**Bench Outreach Pipeline — design document v0.4, 16 Sep 2026.**
Prepared by Saroja Nemmaluri for Swaroop Venkatagiri, Bench Sales.
Marked "Draft — steps 1–6 agreed".

The PDF itself is not committed (it is a deliverable, not code). This file records
what the build is being verified against. **It is a transcript of the document, not
an approval.** Anything here can be overridden by the user at the time a step is
built; when that happens, the change is recorded in `docs/STEPS.md` against that step.

## The system — six parts we build

1. **Loader** — puts the names in (300 rows).
2. **HubSpot** — the filing cabinet, single source of truth.
3. **Gateway** — traffic control: decides the next action, routes to the right agent.
4. **Email Agent** — writes and sends personalised emails via the provider.
5. **Research Agent** — looks the person up, enriches, sends details back.
6. **Event Receiver** — takes in replies and bounces, passes them back to Gateway.

Two paid services: the email provider (Mailgun) and a data provider. One real
person: the consultant.

## Step 1 — what goes in (section 02)

Consultant list, Excel, 300 rows: `full_name`, `email` (unique), `phone`,
`technology`, `req_id`. Job requisitions: 5 records in HubSpot, REQ-01 to REQ-05,
entered by hand: `req_id`, `role_title`, `required_skills`, `location`, `terms`.

Every consultant belongs to exactly one requisition via `req_id`. **The system does
no matching** — each consultant is emailed only about their one role. Five is
today's number of requisitions; it can change.

## Step 2 — Loader

Reads the sheet. Per row: create a contact, or update the existing contact when the
email already exists in HubSpot. Saves `full_name`, `email`, `phone`, `technology`,
`req_id`; links the contact to the requisition record with the same `req_id`.
A row whose `req_id` matches no requisition is listed as "no matching requisition"
and is not emailed until fixed. New contacts start at stage `loaded`; existing
contacts keep their stage. Anyone who cannot be saved is listed with the reason.

Result: 300 contacts in HubSpot, each linked to one requisition.

## Step 3 — HubSpot tells the Gateway there is work to do

Loading a contact alerts the Gateway automatically. The alert carries only the
contact's HubSpot ID. The Gateway verifies the alert really came from HubSpot, then
reads `qualification_stage`:

- `loaded` + linked to a requisition → give the Email Agent a "send Email #1" job
- `loaded` + no matching requisition → wait
- any other stage → do nothing

A duplicate alert is acted on only once. If the Email Agent fails, the Gateway keeps
the job and retries.

**Stages:** loaded → emailed → engaged → researched → followed_up → qualified →
handed_off. Closing stages: suppressed, closed, invalid, referred.

**Rule:** only `loaded` contacts start the flow. Consultants further along, closed or
suppressed are never restarted; a paused consultant returns to `loaded` when the
pause date passes.

## Step 4 — Email Agent writes Email #1

Reads the contact and its linked requisition. Picks two skills: the first two of the
consultant's skills that the role requires, or the first skills they list if fewer
than two match. Fills the one fixed approved template (section 05). Creates a new
`ref_id` for this email; every email gets its own.

Blanks: first name ← `full_name`; two skills ← `technology` checked against required
skills; role/city/state/client industry ← linked requisition; assistant name,
company, postal address ← fixed settings (**to be decided**); unsubscribe link and
reference number ← made per email.

## Step 5 — Email #1 sent through Mailgun

Stage is re-read right before sending; anyone opted out is skipped. At most **50
emails a day** leave the sending address, shared evenly across the 5 requisitions.
Order: Email #2s first, then reminders, then new Email #1s — so the first emails
reach all 300 in about 6 days. The email goes to Mailgun with its reference number;
if Mailgun does not accept it, 3 more tries, then `email_1_status = failed` with the
reason. Once accepted, `email_1_sent_at` and `email_1_ref_id` are saved on the
contact and the Gateway moves the stage `loaded` → `emailed`. Opens and clicks are
recorded for reports only and never move anyone forward.

## Step 6 — what comes back from Mailgun

Delivery notices: delivered → `email_1_status = delivered`. Permanent bounce →
status bounced, stage `invalid`, reason "bad data: email bounced", never emailed
again. Temporary failure → Mailgun retries; if it finally fails, handled as a bounce.
Unsubscribed → stage `suppressed`, permanently, every campaign. Marked as spam →
treated as an unsubscribe (**to confirm**). Opened or clicked → reports only.

Replies: Mailgun passes each reply to the Event Receiver and copies a team Outlook
mailbox. The Event Receiver verifies the reply really came through Mailgun, then
matches it to the consultant and the email — by reference number, then by
"in reply to", then by sender address (**to confirm**). Anything still unmatched goes
to a list for a person to check. The full reply and attachments are saved on the
contact in HubSpot. It tells the Gateway who replied and to which email, and
confirms receipt to Mailgun within 5 seconds; if it does not, Mailgun resends later.

## Steps 7+ — still under review

What a reply means, the reminder to people who do not reply, research, Email #2,
qualification and handoff. The sections below are **as first drafted only** and must
be re-agreed before being built.

### Reply branches (section 04)
- Interested (clear positive intent) → Research Agent runs, then Email #2
- Referral (points at someone else) → recorded on the contact, flagged for TA
- Not interested (opt-out words or click) → suppressed permanently, every campaign
- Pause (PAUSE + a duration) → suppressed until that date, then eligible again
- Auto-reply (out-of-office headers) → not counted, retried after the return date
- No reply after 4 days → one reminder email; still silent, then closed
- Hard bounce → address marked invalid, flagged bad data

Opening an email or clicking a link never qualifies anyone. Qualification takes a
second reply that answers the questions in Email #2.

### Email #1 template (section 05)
From `assistant_name` — `company`. Subject: `top_skill` background — `role_title` role.

> Hello `first_name`, your `top_skill` work, along with `second_skill`, is why I
> wanted to flag the `role_title` role. If it's not for you, I'd appreciate any
> pointers. Worth a quick look?
>
> For transparency, I'm `assistant_name`, an automated recruiting assistant, and I'm
> sourcing for a `role_title` position in `city`, `state` with a `client_industry`
> company.
>
> `assistant_name` — Recruiting Coordinator

Footer: `company` · `postal_address` · Unsubscribe · Reply "PAUSE" with a duration ·
Ref `ref_id`. Final wording, assistant name, company name and postal address are
**still to be decided**.

### Email #2 template (section 06)
Sent only after research has run; this is the email that qualifies. Subject:
Re: `top_skill` background — `role_title` role. Contains: "You look like a strong fit
on" ← `skills_matched`; "A few areas I couldn't confirm from your profile" ←
`skills_gap` (asked, never assumed); and six questions — current location (city and
state), can they work `days` days onsite in `city`, W2-only hourly rate target,
latest resume attached, visa sponsorship now or in future, years of overall
experience with this skillset.

### The contact record afterwards (section 07)
`technology`, `req_id` (Loader); `email_1_sent_at`, `email_1_ref_id` (Email Agent);
`email_1_status` (Event Receiver); `reply_status`; `research_status`,
`skills_matched`, `skills_gap` (Research Agent); `reminder_ref_id`,
`email_2_sent_at`, `email_2_ref_id` (Email Agent); `qualification_stage` (Gateway);
`suppressed_until`; `closed_reason`; `ta_owner` (assigned on qualification).

## Guardrails (section 08)

- Every email carries a physical postal address and a working unsubscribe link.
- Opt-outs are global, permanent, honoured within one send cycle.
- Max 50 emails a day, from a separate Mailgun address warmed up for 2–3 weeks first.
- No fabricated engagement — delivered, bounced and replied come only from Mailgun's
  signed notices.
- Nobody dropped silently — anyone unworkable gets a written reason on their record.
- LinkedIn is not scraped. Research uses a licensed data provider or publicly
  published pages only.

## Not in Phase 1 (section 10)

SMS and voice (email only); booking the recruiter call (agent hands off, a human
books); scraping LinkedIn or any source forbidding automated collection; changing
LQABR (this is a new repository, LQABR is reference only); a second system of record.

## Phase 1 is done when (section 12)

- All 300 load into HubSpot, and every one either receives Email #1 or carries a
  written reason why not
- A positive reply reliably produces a research record and Email #2 within the
  agreed window
- An opt-out stops every further email within one send cycle
- Qualified leads arrive with all six answers captured and a TA owner assigned
- The numbers reconcile: loaded = sent + suppressed + failed, and every qualified
  lead replied twice
- No engagement is recorded that the email provider did not report
