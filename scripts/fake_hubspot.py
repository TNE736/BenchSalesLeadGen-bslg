"""Fire a correctly signed HubSpot webhook at a local gateway — no tunnel needed.

    python scripts/fake_hubspot.py                          # decision_maker = Yes → routed
    python scripts/fake_hubspot.py --value No               # discarded (200)
    python scripts/fake_hubspot.py --bad-signature          # 401
    python scripts/fake_hubspot.py --object-id 123 --redeliver   # attemptNumber=1 → duplicate

Reads HUBSPOT_APP_CLIENT_SECRET and GATEWAY_PUBLIC_URL from .env (or the environment).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bench_outreach.common.settings import env            # noqa: E402
from bench_outreach.gateway.signature import compute_v3   # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=env("GATEWAY_PUBLIC_URL") or "http://localhost:8080")
    p.add_argument("--property", default="decision_maker")
    p.add_argument("--value", default="Yes")
    p.add_argument("--object-id", default="101")
    p.add_argument("--event-id", default=None)
    p.add_argument("--redeliver", action="store_true", help="send with attemptNumber=1")
    p.add_argument("--bad-signature", action="store_true")
    args = p.parse_args()

    secret = env("HUBSPOT_APP_CLIENT_SECRET")
    if not secret:
        print("HUBSPOT_APP_CLIENT_SECRET is not set (put the app's client secret in .env)")
        return 2

    event = {
        "eventId": args.event_id or str(uuid.uuid4().int % 10**12),
        "subscriptionId": 1,
        "portalId": 247408852,
        "appId": 1,
        "occurredAt": int(time.time() * 1000),
        "subscriptionType": "contact.propertyChange",
        "attemptNumber": 1 if args.redeliver else 0,
        "objectId": int(args.object_id),
        "propertyName": args.property,
        "propertyValue": args.value,
        "changeSource": "CRM_UI",
    }
    body = json.dumps([event], separators=(",", ":"))
    uri = args.url.rstrip("/") + "/hubspot/events"
    ts = str(int(time.time() * 1000))
    sig = compute_v3(secret, "POST", uri, body, ts)
    if args.bad_signature:
        sig = "AAAA" + sig[4:]

    resp = httpx.post(uri, content=body, headers={
        "Content-Type": "application/json",
        "X-HubSpot-Signature-v3": sig,
        "X-HubSpot-Request-Timestamp": ts,
    }, timeout=30)
    print(resp.status_code)
    print(json.dumps(resp.json(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
