# Salesforce

Background on the platform itself. Every Salesforce lead gets this, whatever their
title — the role-specific material comes from `skills.json`, not from here.

Salesforce is a CRM platform that almost nobody uses as shipped. Companies extend it
heavily, and that extension work is what the people we email do for a living. An org
is a living system: it accumulates automation, custom objects, integrations and
technical debt, and the people who work on it are usually maintaining somebody else's
decisions as much as making their own.

## How the work divides

Roughly four kinds of work, and most orgs need all of them:

**Configuration** — objects, fields, Flow automation, permissions, reports. Done
without code, and it covers more of a typical org than outsiders expect.

**Development** — Apex for business logic the platform can't express declaratively,
Lightning Web Components for interfaces, and the integrations that connect the org to
everything else.

**Analysis and design** — deciding what the business actually needs, and at the
senior end deciding the data model, the security model, and what gets solved with
configuration versus code.

**Assurance** — testing that config and code together still do what was asked, which
matters more here than on most platforms because Salesforce ships three major
releases a year and every one of them can break something.

The line between these blurs constantly. A developer in a small team does admin work;
a BA usually configures as well as documents; an architect still reviews code. Never
assume a title means someone only does one of the four.

## The two things every consultant weighs

**Greenfield or maintenance.** Building something new, or inheriting an org with ten
years of accumulated decisions in it. Consultants care about this more than almost
anything else, and the answer changes whether a role is attractive.

**Sole or team.** Being the only Salesforce person in the business is a very
different job from being one of eight.

## Vocabulary to get right

"Org", not "instance" or "environment". **Flow** has replaced Process Builder and
Workflow Rules; writing either dates the sender badly. **Lightning Web Components**
is current, **Aura** is legacy, **Visualforce** is older still but alive in
maintenance work. **Apex** is the server-side language; **SOQL** is the query
language. Sandboxes are for development, production is the live org.

**Data Cloud is now Data 360**, renamed at Dreamforce 2025.

**Profiles have not been retired.** Salesforce announced that permissions in profiles
would be retired, then cancelled the enforcement in June 2026. Consultants lived
through that reversal — never write anything implying profiles are being switched off.
Permission sets and permission set groups are still the direction of travel, just
without a deadline.

**Sales Cloud and Marketing Cloud are genuinely different products** with different
data models, different languages and a separate login. Marketing Cloud Engagement came
out of the ExactTarget acquisition and connects to core Salesforce rather than living
inside it. Treating a Marketing Cloud specialist as a Salesforce developer is the
fastest way to lose them.

**Salesforce CPQ is end-of-sale, not end-of-life** — still supported, still sold to
existing customers, with Revenue Cloud Advanced as the successor. Never imply CPQ
skills are obsolete.
