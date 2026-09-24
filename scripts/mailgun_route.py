"""Create, update or inspect the Mailgun route that catches candidate replies.

    python scripts/mailgun_route.py              # dry run: shows what it would do
    python scripts/mailgun_route.py --apply      # creates it, or corrects the URL
    python scripts/mailgun_route.py --show       # every route on the account
    python scripts/mailgun_route.py --inspect    # a route in full + has it caught anything?
    python scripts/mailgun_route.py --history    # EVERY event Mailgun still holds, oldest first
    python scripts/mailgun_route.py --plan       # what this account's plan and limits actually are
    python scripts/mailgun_route.py --delete     # removes ours (needs --apply too)

WHY THIS EXISTS. A reply from a candidate lands in a mailbox, where no program
can see it. A Mailgun route is a standing instruction -- "mail for this address,
do these things" -- that makes a copy arrive somewhere the gateway can read,
while a second copy still reaches the human inbox exactly as it does today.

WHAT IT CONFIGURES. One route, and nothing else. Not DNS, not the domain, not
sending, not the delivered/opened/bounced webhooks, not a key.

    when   match_recipient("^reply-(\\d+)@<domain>$")
    do     forward(<gateway>/mailgun/inbound)   <- the system's copy
           forward(<your mailbox>)              <- your copy, unchanged
           stop()                               <- no other rule runs

THE NGROK CHORE. A route stores a fixed URL, and ngrok's changes on restart.
So this UPDATES rather than duplicates: it finds our route by its description
and corrects the URL in place. Run the same line after every restart.

SAFE BY DESIGN:
  * dry run is the default; --apply is the only thing that writes
  * it touches exactly one route, found by description, and never another
  * creating it changes nothing observable until `reply_to` sends mail to that
    address, which is a separate change
  * the API key is read from .env and never printed
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import httpx                                                            # noqa: E402

from bench_outreach.common.settings import env                          # noqa: E402

API = "https://api.mailgun.net/v3/routes"
#: How we find our own route again. Changing this orphans the old one.
#: ASCII on purpose: an em dash here drew a 400 from the API with no
#: explanation, and a description is not worth a character that might.
DESCRIPTION = "Bench Outreach - candidate replies"
INBOUND_PATH = "/mailgun/inbound"


def expression(domain: str) -> str:
    """Mail to `reply-<digits>@<domain>`, and nothing else. A plain address on
    the same domain -- stephen@reply.tekninjas.com -- is untouched."""
    return f'match_recipient("^reply-(\\d+)@{domain.replace(".", chr(92) + ".")}$")'


def actions(gateway: str, mailbox: str) -> list[str]:
    return [f'forward("{gateway.rstrip("/")}{INBOUND_PATH}")',
            f'forward("{mailbox}")',
            "stop()"]


def client(key: str) -> httpx.Client:
    return httpx.Client(auth=("api", key), timeout=30.0)


def every_route(http: httpx.Client) -> list[dict]:
    got, skip = [], 0
    while True:
        page = http.get(API, params={"limit": 100, "skip": skip}).raise_for_status().json()
        items = page.get("items", [])
        got += items
        if len(items) < 100:
            return got
        skip += 100


def refused(exc: httpx.HTTPStatusError) -> int:
    """Mailgun's own explanation, verbatim.

    The first version of this script called `raise_for_status()` and printed a
    bare traceback, so a 400 said "Bad Request" and nothing about which field
    was bad. The body always says. Print it.
    """
    code = exc.response.status_code
    print(f"\nMailgun refused the request: HTTP {code}")
    try:
        detail = exc.response.json()
        print("  " + json.dumps(detail, indent=2).replace("\n", "\n  "))
    except ValueError:
        print("  " + (exc.response.text or "(no body)")[:1000])
    if code == 401:
        print("\n  MAILGUN_API_KEY is wrong, or this is an EU account -- then the")
        print("  base URL is https://api.eu.mailgun.net/v3/routes")
    if code == 402:
        print("\n  402 is a plan limit: routes may not be included in this plan.")
    return 1


EVENTS = "https://api.mailgun.net/v3/{domain}/events"


def inspect(key: str, domain: str, route_id: str) -> int:
    """One route, in full -- and whether it is LIVE or abandoned.

    The route object says what a route WOULD do. It does not say whether any
    mail has ever hit it. `store()` and `forward()` both raise events, so the
    events log answers the question the route object cannot: is anyone using
    this, and when did mail last arrive?
    """
    with client(key) as http:
        try:
            route = http.get(f"{API}/{route_id}").raise_for_status().json()
        except httpx.HTTPStatusError as exc:
            return refused(exc)
        print("THE ROUTE, IN FULL\n")
        print(json.dumps(route, indent=2))

        print(f"\n\nHAS THIS ROUTE EVER CAUGHT ANYTHING?  ({domain})\n")

        #: `accepted` counts BOTH directions -- an outbound message this domain
        #: sent is "accepted" too. The first version of this printed our own
        #: outgoing emails and announced the route was live. The direction is in
        #: the recipient: inbound mail is addressed TO this domain.
        def inbound(item: dict) -> bool:
            return str(item.get("recipient", "")).lower().endswith("@" + domain.lower())

        caught = 0
        for kind in ("stored", "accepted", "rejected", "failed"):
            try:
                page = http.get(EVENTS.format(domain=domain),
                                params={"event": kind, "limit": 100, "ascending": "no"}
                                ).raise_for_status().json()
            except httpx.HTTPStatusError as exc:
                print(f"  {kind:9} could not read (HTTP {exc.response.status_code})")
                continue
            items = page.get("items", [])
            coming_in = [i for i in items if inbound(i)]
            going_out = len(items) - len(coming_in)
            caught += len(coming_in)
            note = f"  ({going_out} outbound, not this route's business)" if going_out else ""
            print(f"  {kind:9} {len(coming_in)} inbound{note}")
            for i in coming_in[:8]:
                print(f"      {when_of(i)}  from {headers_of(i).get('from', '?')[:50]}")
                print(f"{'':>24}  to   {i.get('recipient', '?')[:50]}")
                print(f"{'':>24}  subj {str(headers_of(i).get('subject', ''))[:50]}")

        print()
        if caught:
            print("  LIVE. Mail has arrived for this route. Do not change it without asking")
            print("  whoever owns the system it points at.")
        else:
            print("  NOTHING. No mail has ever arrived for this route in the events Mailgun")
            print("  still retains. It is configured, not in use.")
            print("  (Retention is finite -- this proves the recent past, not all time.)")
    return 0


#: Read-only, all of them. Mailgun's API reference does not clearly document an
#: endpoint that names the account's plan, so rather than reason from a published
#: pricing page and call it proof, ask the account and print what it says.
PLAN_PROBES = [
    ("account limits (v5)",      "https://api.mailgun.net/v5/accounts/limits"),
    ("account (v5)",             "https://api.mailgun.net/v5/accounts"),
    ("account limits (v1)",      "https://api.mailgun.net/v1/accounts/limits"),
    ("custom monthly limit",     "https://api.mailgun.net/v5/accounts/limit/custom/monthly"),
    ("subaccounts (tier hint)",  "https://api.mailgun.net/v5/accounts/subaccounts"),
    ("this domain (v4)",         "https://api.mailgun.net/v4/domains/{domain}"),
    ("all domains (v4)",         "https://api.mailgun.net/v4/domains"),
]


def plan(key: str, domain: str) -> int:
    """What this account's plan and limits actually are, from the account itself."""
    print("WHAT THE ACCOUNT SAYS ABOUT ITSELF\n")
    with client(key) as http:
        for label, url in PLAN_PROBES:
            url = url.format(domain=domain)
            try:
                resp = http.get(url)
            except httpx.HTTPError as exc:
                print(f"  {label:26} unreachable ({type(exc).__name__})")
                continue
            if resp.status_code != 200:
                print(f"  {label:26} HTTP {resp.status_code}")
                continue
            try:
                body = resp.json()
            except ValueError:
                print(f"  {label:26} HTTP 200, not JSON")
                continue
            text = json.dumps(body)
            interesting = [w for w in ("plan", "tier", "subscription", "quota", "limit",
                                       "free", "basic", "foundation", "scale", "trial")
                           if w in text.lower()]
            print(f"  {label:26} HTTP 200" + (f"   mentions: {', '.join(interesting)}" if interesting else ""))
            print("      " + json.dumps(body, indent=2)[:1400].replace("\n", "\n      "))
            print()

    print("\nWHAT WE KNOW FOR CERTAIN, WITHOUT INFERENCE\n")
    print("  This account refused a second route with:")
    print('      {"message": "Routes quota (1) is exceeded"}')
    print("  So the inbound route limit on this account is 1. That is measured,")
    print("  not deduced. Mailgun's published pricing puts 1 route on Free and 5 on")
    print("  Basic -- but a legacy or custom plan could also cap at 1, so the LIMIT")
    print("  is proven and the plan NAME is not. The dashboard, under Account ->")
    print("  Billing, is the only authority on the name.")
    return 0


def history(key: str, domain: str) -> int:
    """Every event Mailgun still holds for this domain, oldest first.

    Mailgun keeps event data "up to 30 days, depending on plan level", so this
    cannot reach back to a route created in June -- that history is gone from
    everywhere, not just from this query. What it CAN do is find the real
    boundary: the oldest record that still exists. Anything before it is
    unknowable, and saying so is better than implying the window is the whole
    story.
    """
    inbound_rows, outbound, oldest, newest, pages = [], 0, None, None, 0
    with client(key) as http:
        url = EVENTS.format(domain=domain)
        params = {"limit": 300, "ascending": "yes",
                  "begin": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())}
        try:
            while url and pages < 50:
                page = http.get(url, params=params).raise_for_status().json()
                params = None                       # the paging URL carries its own
                items = page.get("items", [])
                if not items:
                    break
                pages += 1
                for i in items:
                    ts = i.get("timestamp")
                    if isinstance(ts, (int, float)):
                        oldest = min(oldest or ts, ts)
                        newest = max(newest or ts, ts)
                    if str(i.get("recipient", "")).lower().endswith("@" + domain.lower()):
                        inbound_rows.append(i)
                    else:
                        outbound += 1
                url = (page.get("paging") or {}).get("next")
        except httpx.HTTPStatusError as exc:
            return refused(exc)

    total = len(inbound_rows) + outbound
    print(f"EVERY EVENT MAILGUN STILL HOLDS FOR {domain}\n")
    print(f"  {total} event(s) across {pages} page(s)")
    if oldest:
        print(f"  oldest retained  {datetime.fromtimestamp(oldest, timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
        print(f"  newest retained  {datetime.fromtimestamp(newest, timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
        days = (newest - oldest) / 86400
        print(f"  the window is {days:.1f} day(s) wide — this is the retention boundary,")
        print(f"  and nothing before it exists to be checked, here or anywhere.\n")
    print(f"  outbound (mail this domain SENT)      {outbound}")
    print(f"  inbound  (mail that ARRIVED, i.e. the route's business)  {len(inbound_rows)}\n")
    for i in inbound_rows[:40]:
        print(f"    {when_of(i)}  {i.get('event','?'):9} to {i.get('recipient','?')[:45]}")
        print(f"{'':>24}  from {headers_of(i).get('from', '?')[:45]}")
    if not inbound_rows:
        print("    Not one inbound message in the whole retained window.")
    return 0


def when_of(item: dict) -> str:
    ts = item.get("timestamp")
    if not isinstance(ts, (int, float)):
        return "?"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def headers_of(item: dict) -> dict:
    return ((item.get("message") or {}).get("headers") or {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--show", action="store_true", help="list every route on the account")
    ap.add_argument("--delete", action="store_true", help="remove our route")
    ap.add_argument("--inspect", metavar="ID",
                    help="one route in full, plus the inbound mail it has actually caught")
    ap.add_argument("--history", action="store_true",
                    help="walk every retained event, oldest first, and show the retention boundary")
    ap.add_argument("--plan", action="store_true",
                    help="probe every endpoint that might name this account's plan or limits")
    args = ap.parse_args()

    key = env("MAILGUN_API_KEY")
    domain = env("MAILGUN_DOMAIN")
    gateway = env("GATEWAY_PUBLIC_URL")
    mailbox = env("MAILGUN_REPLY_TO")
    missing = [n for n, v in (("MAILGUN_API_KEY", key), ("MAILGUN_DOMAIN", domain),
                              ("GATEWAY_PUBLIC_URL", gateway),
                              ("MAILGUN_REPLY_TO", mailbox)) if not v]
    if missing:
        print("missing in .env: " + ", ".join(missing))
        return 2

    try:
        with client(key) as http:
            routes = every_route(http)
    except httpx.HTTPStatusError as exc:
        return refused(exc)
    except httpx.HTTPError as exc:
        print(f"could not reach Mailgun: {type(exc).__name__}: {exc}")
        return 1

    if args.inspect:
        return inspect(key, domain, args.inspect)

    if args.history:
        return history(key, domain)

    if args.plan:
        return plan(key, domain)

    if args.show:
        print(f"{len(routes)} route(s) on this account\n")
        for r in routes:
            print(f"  {r.get('description') or '(no description)'}   priority {r.get('priority')}")
            print(f"      when  {r.get('expression')}")
            for a in r.get("actions", []):
                print(f"      do    {a}")
            print(f"      id    {r.get('id')}\n")
        return 0

    ours = next((r for r in routes if r.get("description") == DESCRIPTION), None)
    want = {"priority": "0", "description": DESCRIPTION,
            "expression": expression(domain), "action": actions(gateway, mailbox)}

    if args.delete:
        if not ours:
            print("nothing to delete: no route with our description")
            return 0
        print(f"would DELETE route {ours['id']}  ({DESCRIPTION})")
        if not args.apply:
            print("\ndry run — add --apply to delete it")
            return 0
        try:
            with client(key) as http:
                http.delete(f"{API}/{ours['id']}").raise_for_status()
        except httpx.HTTPStatusError as exc:
            return refused(exc)
        print("deleted")
        return 0

    print(f"domain   {domain}")
    print(f"gateway  {gateway}{INBOUND_PATH}")
    print(f"mailbox  {mailbox}\n")
    print(f"when  {want['expression']}")
    for a in want["action"]:
        print(f"do    {a}")

    if ours is None:
        print(f"\nno route named {DESCRIPTION!r} — would CREATE it")
    elif ours.get("expression") == want["expression"] and ours.get("actions") == want["action"]:
        print(f"\nroute {ours['id']} already says exactly this — nothing to do")
        return 0
    else:
        print(f"\nroute {ours['id']} exists and differs — would UPDATE it in place")
        for a in ours.get("actions", []):
            print(f"  was   {a}")

    if not args.apply:
        print("\ndry run — add --apply to write it")
        return 0

    try:
        with client(key) as http:
            if ours is None:
                made = http.post(API, data=want).raise_for_status().json()
                print(f"\ncreated route {made.get('route', {}).get('id')}")
            else:
                http.put(f"{API}/{ours['id']}", data=want).raise_for_status()
                print(f"\nupdated route {ours['id']}")
    except httpx.HTTPStatusError as exc:
        return refused(exc)
    print("Mailgun will now POST a copy of every matching reply to the gateway,")
    print(f"and forward a copy to {mailbox} as before.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
