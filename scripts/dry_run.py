"""Work one real contact end to end, without ngrok, the gateway, or sending anything.

    python scripts/dry_run.py 551358867185
    python scripts/dry_run.py 551358867185 --ignore-guards   # re-run an already-SENT contact

Reads the contact from HubSpot through the MCP, hands it to Claude with the skill
catalogue and tools, prints the email and appends it to logs/email_agent/.
Writes NOTHING to HubSpot and sends NOTHING.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bench_outreach.common.hubspot_mcp import HubSpotMCPError, NotAuthorised  # noqa: E402
from bench_outreach.common.settings import env                                # noqa: E402
from bench_outreach.email_agent import email_agent_logging as elog                          # noqa: E402
from bench_outreach.email_agent.agent import EmailAgent, load_config          # noqa: E402
from bench_outreach.email_agent.sender import DryRunSender                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("object_id", help="HubSpot contact id")
    ap.add_argument("--ignore-guards", action="store_true",
                    help="skip the already-contacted / decision-maker checks")
    args = ap.parse_args()

    if not env("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set in .env")
        return 2

    config = load_config(REPO / "config" / "email.yaml")
    if args.ignore_guards:
        config["guards"]["skip_if_status_in"] = []
        config["guards"]["require_decision_maker"] = False

    out = REPO / "artifacts" / "email_agent" / f"drafts-{date.today().isoformat()}.md"
    try:
        agent = EmailAgent(sender=DryRunSender(out), config=config, dry_run=True)
    except ValueError as exc:
        print(f"skill problem: {exc}")
        return 2

    print(f"contact {args.object_id}  ·  model {config['model']['name']}  ·  DRY RUN\n")
    started = time.perf_counter()
    try:
        #: Input 1, the second start: a command-line invocation. One run = one
        #: contact, and the trace id is born here -- there is no gateway.
        elog.configure_logging(folder=str(REPO / "logs" / "email_agent"))
        with elog.run("cli dry_run", object_id=args.object_id):
            outcome = agent.work(args.object_id, trigger_id="dry-run")
            elog.finish(**outcome.as_dict())
    except NotAuthorised as exc:
        print(f"HubSpot: {exc}")
        return 1
    except HubSpotMCPError as exc:
        print(f"HubSpot unreachable: {exc}")
        return 1

    took = time.perf_counter() - started
    print(f"status   : {outcome.status}")
    if outcome.reason:
        print(f"reason   : {outcome.reason}")
    if outcome.status == "sent":
        print(f"files read: {outcome.reference}")
        print(f"subject  : {outcome.subject}")
        print(f"\nfull email appended to {out.relative_to(REPO)}")
        tail = out.read_text(encoding="utf-8").split("=" * 78)[-1]
        print("-" * 70 + tail.rstrip() + "\n" + "-" * 70)
    print(f"\ntook {took:.1f}s · nothing was sent · HubSpot unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
