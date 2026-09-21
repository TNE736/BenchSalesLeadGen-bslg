# Oracle

Background on the Oracle ecosystem itself. Every Oracle lead gets this, whatever their
title — the role-specific material comes from `assets/oracle.json`, not from here.

"Oracle" is not one technology. It is a vendor whose products sit in three largely
separate worlds, staffed by people who mostly cannot do each other's jobs. Treating
them as one field is the fastest way to write an email that gets deleted.

## The three worlds

**Fusion Cloud Applications** — the SaaS ERP, HCM and SCM suite. This is the centre
of the 2026 contract market. Consultants here are functional or techno-functional:
they run fit-gap workshops, configure in Functional Setup Manager, load data, build
reports, and regression test the quarterly update. They are not developers in the
usual sense, and calling them that lands badly.

**E-Business Suite** — the on-premise predecessor, still very much alive. Oracle
extended Premier Support for 12.2 through at least 2037, and 12.2.15 shipped in late
2025. EBS people are told their skills are dying roughly once a week and find it
tiresome. The growth work here is upgrades and EBS-to-Fusion migration.

**Database and infrastructure** — DBAs, PL/SQL developers, Exadata and OCI. A
different profession entirely from the applications side. An Oracle DBA and an Oracle
Fusion HCM consultant share an employer's vendor list and nothing else.

## What the work has in common

Across all three, the job is usually maintaining and extending something large that
somebody else built, under a release cadence the consultant does not control. Fusion
updates quarterly (26A, 26B, 26C, 26D). The database patches quarterly. EBS estates
carry a decade of customisation. Being good at this is as much about not breaking
things as building them.

## Vocabulary to get right

These are the errors an Oracle consultant notices immediately.

**Never write "Oracle 23c" or "Oracle 26c".** Oracle Database 23ai was renamed
**Oracle AI Database 26ai** in October 2025. The suffix is `ai`, never `c`. And 19c is
still the production workhorse for most estates — referring to it is not dated.

**OIC and OCI are different things.** Oracle Integration Cloud is the iPaaS that
connects Fusion to other systems. Oracle Cloud Infrastructure is the IaaS platform.
One letter apart, entirely different people. Getting this wrong in a subject line is
fatal, and it happens constantly.

**Fusion Applications and Fusion Middleware are different things.** Fusion
Applications is the SaaS suite. Fusion Middleware is WebLogic and SOA Suite — on-prem
Java infrastructure. This is the classic outsider error.

**"Oracle Cloud" on its own is ambiguous.** To an infrastructure person it means OCI.
To an applications person it means Fusion SaaS. Always qualify it.

**The data loaders are pillar-specific.** HDL and HSDL are HCM only. FBDI is the
ERP and SCM equivalent. They are never interchangeable, and it is FBDI — File-Based
Data Import — not "FBDL".

**Fusion releases are 26A/26B/26C/26D**, quarterly. Not "R13", which stopped being
how Fusion is versioned years ago.

**EBS is not end-of-life**, and saying or implying it is will lose the reply.

**Do not lean on certifications for EBS.** Oracle's current catalogue lists none, so
most senior EBS people hold no current credential and it is not a signal in that
family. Lead with module and version instead. For Fusion, certification names carry a
year that rolls annually — someone certified in 2024 holds a 2024 credential, and
that is correct for its vintage, not out of date.
