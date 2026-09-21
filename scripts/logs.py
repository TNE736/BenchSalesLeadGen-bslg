"""Read the agent logs. The one command for "what actually happened?"

    python scripts/logs.py                    the last run, in full
    python scripts/logs.py --trace            ONE lead end to end, both services
    python scripts/logs.py --full             ...and the prompt, reply and email text
    python scripts/logs.py --runs             every run, one line each
    python scripts/logs.py --run trg-5a748b   one run by id (a prefix is enough)
    python scripts/logs.py --lead 551358867185   every run for one contact
    python scripts/logs.py --audit            AUDIT only: outbound calls and cost
    python scripts/logs.py --system           SYSTEM only: startup, config, what loaded
    python scripts/logs.py --process          PROCESS only: the decisions for each lead
    python scripts/logs.py --failures         only runs that did not send
    python scripts/logs.py --raw              the underlying JSON, unformatted
    python scripts/logs.py --gateway          read the gateway's log instead

Every record carries exactly one of three STREAMS, printed in its own column so
you can always see which kind of log you are looking at:

    system   about the SERVICE — startup, configuration, which skill files were
             loaded. True before any lead exists. "Is this thing set up right?"
    process  about the WORK, for one lead — the steps, the guards, the skill that
             was invoked, the draft. "What did we decide, and why?"
    audit    about the CALLS that left this process — HubSpot, Anthropic, Mailgun:
             endpoint, status, duration, tokens. "What did it cost?"

The rule that keeps them apart: audit records what a call COST, process records
what it PRODUCED, system records what the service IS.

Nothing here writes. It is safe to run against a live log at any time.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHOW_FULL = False
STREAMS = ("system", "process", "audit")
LOGS = {"email_agent": REPO / "logs" / "email_agent",
        "gateway": REPO / "logs" / "gateway"}

#: Keys already shown in the rendered line, so printing them again is noise.
_SHOWN = {"ts", "service", "trace_id", "stream", "event", "object_id", "trigger_id",
          "step", "label", "detail", "title", "endpoint", "ok", "status",
          "duration_ms", "input_tokens", "output_tokens", "summary"}


def load(directory: Path, only: str = "") -> list[dict]:
    """Every record from a service's stream files, oldest first.

    Three files now, not one. A caller asking for "the log" means all of it — the
    split exists so you CAN read one stream, not so you have to.
    """
    names = (only,) if only else STREAMS
    records, found = [], []
    for stream in names:
        path = directory / f"{stream}.jsonl"
        if not path.exists():
            continue
        found.append(path.name)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  ! {path.name} line {n} is not valid JSON, skipped")
    if not found:
        raise SystemExit(f"no logs in {directory.relative_to(REPO)} — nothing has run yet")
    return sorted(records, key=lambda r: r.get("ts", 0))


DESCRIBES = {
    "system": "about the SERVICE: startup, configuration, what was loaded",
    "process": "about the WORK on one lead: steps, guards, the skill, the draft",
    "audit": "about the CALLS that left this process: status, duration, tokens",
}
LEGEND = ("streams:  system = the service   process = the work on one lead   "
          "audit = calls that left this process")


def clock(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S")


def took(ms) -> str:
    return "     -" if ms is None else f"{ms / 1000:6.1f}s"


def group(records: list[dict]) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for r in records:
        runs.setdefault(r.get("trace_id", "?"), []).append(r)
    return runs


def headline(trace_id: str, rs: list[dict]) -> str:
    end = next((r for r in reversed(rs) if r.get("event") == "run_finished"), {})
    lead = next((r.get("object_id") for r in rs if r.get("object_id")), "-")
    status = end.get("status") or end.get("summary") or "unfinished"
    return (f"{clock(rs[0]['ts'])}  {trace_id:22} lead {str(lead):14} "
            f"{took(end.get('duration_ms')):>8}  {status}")


def extras(r: dict) -> str:
    # An empty error or reason is the normal case; printing it is noise.
    rest = {k: v for k, v in r.items()
            if k not in _SHOWN and v not in ("", None, [], {})}
    if not rest:
        return ""
    bits = []
    for k, v in rest.items():
        if isinstance(v, list):
            v = f"[{len(v)}] " + ", ".join(str(x) for x in v[:4]) + (" ..." if len(v) > 4 else "")
        bits.append(f"{k}={v}")
    return "\n".join("            " + b for b in bits)


def compact(r: dict, width: int = 96) -> str:
    """The record's own fields on one line, for events that carry no prose."""
    bits = []
    for k, v in r.items():
        if k in _SHOWN or k == "_who" or v in ("", None, [], {}):
            continue
        bits.append(f"{k}={len(v) if isinstance(v, list) else v}")
    line = "  ".join(bits)
    return line if len(line) <= width else line[:width - 3] + "..."


def render(trace_id: str, rs: list[dict]) -> None:
    print()
    print("=" * 78)
    print(headline(trace_id, rs))
    print("=" * 78)
    for r in rs:
        ev, when = r.get("event"), clock(r["ts"])
        if r.get("stream") == "audit":
            money = ""
            if r.get("input_tokens") is not None:
                money = f"   {r['input_tokens']} in / {r['output_tokens']} out"
            mark = "ok " if r.get("ok") else "FAIL"
            print(f"{when}  AUDIT    {r.get('service',''):10} {r.get('endpoint',''):20} "
                  f"{mark} {str(r.get('status') or '-'):>4} {took(r.get('duration_ms'))}{money}")
        elif ev == "step":
            print(f"{when}  ------   {r.get('step')}. {r.get('label','')}")
        elif ev in ("run_started", "run_finished"):
            print(f"{when}  {ev:8} {r.get('title') or r.get('summary','')}")
        else:
            flag = {"fail": "FAIL", "warn": "warn", "ok": "ok  "}.get(ev, ev)
            print(f"{when}  {flag:8} {r.get('detail') or r.get('title','')}")
        tail = extras(r)
        if tail:
            print(tail)


def totals(rs: list[dict]) -> None:
    calls = [r for r in rs if r.get("event") == "outbound_call"]
    if not calls:
        return
    spent = sum(r.get("duration_ms") or 0 for r in calls)
    tin = sum(r.get("input_tokens") or 0 for r in calls)
    tout = sum(r.get("output_tokens") or 0 for r in calls)
    failed = sum(1 for r in calls if not r.get("ok"))
    print(f"\n  {len(calls)} outbound calls, {failed} failed · "
          f"{spent/1000:.1f}s waiting on other people · {tin} in / {tout} out tokens")


def show_transcript(rows: list[dict]) -> None:
    """The actual words: prompt, raw reply, email as sent.

    The JSONL keeps only shas of these, on purpose — see transcript.py. The files
    hold personal data, so they are printed only when asked for with --full.
    """
    paths = []
    for r in rows:
        for key in ("prompt_file", "reply_file", "sent_file"):
            if r.get(key) and r[key] not in paths:
                paths.append(r[key])
    if not paths:
        print("\n  (no transcript for this run — BENCH_RUN_TRANSCRIPTS=false, or it "
              "predates transcripts)")
        return
    for rel in paths:
        target = REPO / rel
        print(f"\n{'-' * 78}\n{rel}\n{'-' * 78}")
        if target.exists():
            print(target.read_text(encoding="utf-8").strip())
        else:
            print("  (file is gone — logs/ is not committed, so it may have been cleared)")
        break                      # all three sections live in the same file


def stitch(prefix: str | None) -> int:
    """One lead's whole journey, across both log files, in time order.

    The two services number their work differently: the gateway mints a trigger id
    per lead (`trg-...`) and the email agent adopts that as its own run id. So the
    gateway's own run id covers a whole webhook DELIVERY — possibly several leads —
    while the trigger id follows ONE lead. The trigger id is therefore the join,
    and this is the only view where the hand-off between the two is visible.
    """
    agent, gateway = load(LOGS["email_agent"]), load(LOGS["gateway"])

    ids = [r["trace_id"] for r in agent
           if str(r.get("trace_id", "")).startswith("trg-")]
    ids = list(dict.fromkeys(ids))
    if prefix:
        ids = [i for i in ids if i.startswith(prefix)]
    if not ids:
        print("no gateway-triggered run found"
              + (f" starting {prefix!r}" if prefix else "")
              + " — only runs that came through the gateway can be traced")
        return 1
    tid = ids[-1]

    # Everything the gateway did in the delivery that minted this trigger, so the
    # signature check and the routing decision are included, not just the hand-off.
    owning = {r.get("trace_id") for r in gateway if r.get("trigger_id") == tid}
    rows = ([dict(r, _who="gateway") for r in gateway if r.get("trace_id") in owning]
            + [dict(r, _who="agent") for r in agent if r.get("trace_id") == tid])
    rows.sort(key=lambda r: r["ts"])

    lead = next((r.get("object_id") for r in rows if r.get("object_id")), "-")
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 1000
    print()
    print("=" * 78)
    print(f"  {tid}   lead {lead}   {span:.1f}s   HubSpot -> gateway -> email agent")
    print(f"  {LEGEND}")
    print("=" * 78)
    print(f"{'time':8}  {'service':8} {'stream':8} {'event':14} what happened")
    print("-" * 78)
    for r in rows:
        ev = r.get("event")
        band = r.get("stream", "process")
        if band == "audit":
            money = (f"   {r['input_tokens']} in / {r['output_tokens']} out"
                     if r.get("input_tokens") is not None else "")
            print(f"{clock(r['ts'])}  {r['_who']:8} {band:8} {'call':14} "
                  f"{r.get('service',''):10}{r.get('endpoint',''):20} "
                  f"{str(r.get('status') or '-'):>4} {took(r.get('duration_ms'))}{money}")
        elif ev == "step":
            print(f"{clock(r['ts'])}  {r['_who']:8} {band:8} {'step':14} "
                  f"{r.get('step')}. {r.get('label','')}")
        else:
            # Events like contact_read carry no prose. Printing the event name
            # twice tells you nothing, so fall back to the fields themselves.
            text = (r.get("detail") or r.get("title") or r.get("summary")
                    or compact(r))
            print(f"{clock(r['ts'])}  {r['_who']:8} {band:8} {str(ev):14} {text}")
    totals(rows)
    if SHOW_FULL:
        show_transcript(rows)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", help="run id, or any prefix of one")
    ap.add_argument("--lead", help="HubSpot contact id")
    ap.add_argument("--runs", action="store_true", help="one line per run")
    ap.add_argument("--audit", action="store_true",
                    help="AUDIT stream only: outbound calls and what they cost")
    ap.add_argument("--system", action="store_true",
                    help="SYSTEM stream only: startup, config, what was loaded")
    ap.add_argument("--process", action="store_true",
                    help="PROCESS stream only: the decisions made for each lead")
    ap.add_argument("--failures", action="store_true", help="runs that did not send")
    ap.add_argument("--raw", action="store_true", help="the JSON itself")
    ap.add_argument("--gateway", action="store_true", help="the gateway log")
    ap.add_argument("--trace", nargs="?", const="", metavar="TRIGGER_ID",
                    help="one lead end to end, gateway and agent in one timeline")
    ap.add_argument("--full", action="store_true",
                    help="also print the prompt, the raw reply and the email as sent")
    ap.add_argument("-n", type=int, default=1, help="how many recent runs (default 1)")
    args = ap.parse_args()

    global SHOW_FULL
    SHOW_FULL = args.full
    if args.trace is not None or args.full:
        return stitch(args.trace or None)

    which = "gateway" if args.gateway else "email_agent"
    records = load(LOGS[which])
    runs = group(records)

    if args.lead:
        runs = {k: v for k, v in runs.items()
                if any(str(r.get("object_id")) == args.lead for r in v)}
        if not runs:
            return print(f"no run touched contact {args.lead}") or 1
    if args.run:
        runs = {k: v for k, v in runs.items() if k.startswith(args.run)}
        if not runs:
            return print(f"no run id starts with {args.run!r}") or 1
    if args.failures:
        runs = {k: v for k, v in runs.items()
                if not any(r.get("status") == "sent" for r in v)}

    if args.runs:
        print(f"{len(runs)} runs in {LOGS[which].relative_to(REPO)}\n")
        for trace_id, rs in runs.items():
            print("  " + headline(trace_id, rs))
        return 0

    only = ("audit" if args.audit else "system" if args.system
            else "process" if args.process else "")
    if only:
        rows = [r for r in records
                if r.get("stream", "process") == only and r.get("trace_id") in runs]
        print(f"\n{only.upper()} stream — {DESCRIBES[only]}\n")
        if not rows:
            print(f"  (nothing on the {only} stream here)")
            return 0
        for r in rows:
            if only == "audit" and r.get("event") == "outbound_call":
                money = (f"   {r['input_tokens']} in / {r['output_tokens']} out"
                         if r.get("input_tokens") is not None else "")
                print(f"{clock(r['ts'])}  {r.get('trace_id',''):34} "
                      f"{r.get('service',''):10} {r.get('endpoint',''):20} "
                      f"{str(r.get('status') or '-'):>4} "
                      f"{took(r.get('duration_ms'))}{money}")
            elif only == "audit":
                # The accountability half of the stream: who was allowed through,
                # who was refused, what we changed. Not a call, so not call columns.
                print(f"{clock(r['ts'])}  {r.get('trace_id',''):34} "
                      f"{str(r.get('event','')):20} {compact(r, 60)}")
            else:
                text = (r.get("detail") or r.get("label") or r.get("title")
                        or r.get("summary") or compact(r))
                print(f"{clock(r['ts'])}  {r.get('trace_id',''):34} "
                      f"{str(r.get('event','')):16} {text}")
        totals(rows)
        return 0

    chosen = list(runs.items())[-args.n:]
    if not chosen:
        print("nothing to show")
        return 1
    for trace_id, rs in chosen:
        if args.raw:
            for r in rs:
                print(json.dumps(r, ensure_ascii=False))
        else:
            render(trace_id, rs)
            totals(rs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
