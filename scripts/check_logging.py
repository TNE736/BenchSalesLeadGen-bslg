"""Run the §11 checks of Logging Spec v3.0 against this repo's logging module.

    python scripts/check_logging.py            # all three modes, C1-C14
    python scripts/check_logging.py --evidence # the same, plus the report blocks

One representative unit of work is driven through `gateway_logging` in each
mode, into its own folder, with the console captured from a pty so §1.1 is
rendered rather than the JSON fallback. Nothing here talks to a network.
"""
from __future__ import annotations

import json
import os
import pty
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODES = ("terse", "normal", "debug")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
PROMPT = ("Research this lead and write the note. " + "The lead is a bench consultant. " * 12)


# --------------------------------------------------------------- the workload
def workload() -> None:
    """One unit of work that touches every part of the spec the checks ask
    about: steps, items with a failure and a skip, three kinds of call, a step
    that raises, and a secret nested five deep."""
    from bench_outreach.gateway import gateway_logging as glog

    glog.configure_logging(
        folder=os.environ["LOG_DIR"], mode=os.environ["LOG_MODE"], version="0.1.0",
        credentials={"ANTHROPIC_API_KEY": "env", "HUBSPOT_MCP_TOKEN_FILE": "file"},
        dependencies={"hubspot": True, "mailgun": True})

    glog.inbound_request(route="/healthz", method="GET", status=200, ok=True,
                         duration_ms=0.4)                       # C4: no run, no id

    with glog.run("POST /hubspot/events", source_ip="216.157.40.33", bytes=308):
        with glog.step("read_leads", source="hubspot") as frame:
            glog.outbound_call(
                peer="hubspot", kind="mcp", operation="get_crm_objects",
                endpoint="https://mcp.hubspot.com", method="POST", status=200,
                duration_ms=180.2, credential_name="HUBSPOT_MCP_TOKEN_FILE",
                request={"objectType": "contacts", "limit": 3},
                response={"keys": ["results"], "text": "3 contacts"})
            frame.ok(leads=3)

        with glog.step("compose") as frame:
            for n, (lead, outcome) in enumerate(
                    [("540690769648", "ok"), ("540690771102", "skipped"),
                     ("540690773311", "failed")], start=1):
                with glog.item(n, 3, object_id=lead) as it:
                    if outcome == "ok":
                        glog.outbound_call(
                            peer="anthropic", kind="model", operation="claude-opus-5",
                            endpoint="https://api.anthropic.com", method="POST",
                            status=200, duration_ms=4120.5,
                            credential_name="ANTHROPIC_API_KEY",
                            request={"model": "claude-opus-5", "max_tokens": 2000,
                                     "prompt": PROMPT, "system": "You are the Email Agent.",
                                     "tools": ["send_email", "flag_lead"], "timeout_s": 600.0},
                            response={"status": 200, "ok": True, "input_tokens": 4321,
                                      "output_tokens": 876, "cache_read_tokens": 2048,
                                      "stop_reason": "end_turn", "tool_uses": 1,
                                      "text": "Hi Ganesh — I saw your Salesforce QA background. " * 8})
                        glog.outbound_call(
                            peer="mailgun", kind="http", operation="POST /messages",
                            endpoint="https://api.mailgun.net", method="POST", status=200,
                            duration_ms=310.0, credential_name="MAILGUN_API_KEY",
                            request={"to": "lead@example.com", "subject": "Bench consultant"},
                            response={"bytes": 118, "content_type": "application/json"})
                        it.ok(email_status="SENT")
                    elif outcome == "skipped":
                        it.skipped("already contacted this week")
                    else:
                        it.failed("MailgunError: 550 rejected")
            frame.ok(composed=1)

        try:                                                    # C6: raises, propagates
            with glog.step("write_back", object_id="540690773311"):
                raise RuntimeError("HubSpot write refused")
        except RuntimeError:
            pass

        with glog.step("redaction_probe") as frame:             # C8: depth 5
            frame.ok(config={"a": {"b": {"c": {"d": {"api_key": "sk-ant-REAL-SECRET",
                                                     "credential_name": "ANTHROPIC_API_KEY",
                                                     "max_tokens": 2000}}}}})
        glog.finish(leads=3, emails_sent=1)


# ------------------------------------------------------------------ the runner
def produce(mode: str, folder: Path) -> str:
    """Run the workload in a child under a pty, and give back its console."""
    #: §1 -- many services, one subfolder per service. The service's own
    #: folder is Input 3, exactly as the gateway passes `logs/gateway`.
    env = {**os.environ, "LOG_DIR": str(folder / "gateway"), "LOG_MODE": mode,
           "NO_COLOR": "1", "PYTHONPATH": str(ROOT / "src")}
    primary, secondary = pty.openpty()
    child = subprocess.Popen([sys.executable, __file__, "--emit"], env=env,
                             stdout=secondary, stderr=subprocess.PIPE, close_fds=True)
    os.close(secondary)
    out = bytearray()
    try:
        while True:
            try:
                chunk = os.read(primary, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        os.close(primary)
    child.wait(timeout=60)
    if child.returncode:
        sys.exit(f"workload failed in {mode}: {child.stderr.read().decode()}")
    return out.decode("utf-8", "replace").replace("\r\n", "\n")


def records(folder: Path, stream: str) -> list[dict]:
    path = folder / "gateway" / f"{stream}.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------------ the checks
def run_checks(data: dict) -> list[tuple[str, bool | None, str]]:
    out: list[tuple[str, bool | None, str]] = []
    normal, folder = data["normal"], data["folders"]["normal"]
    proc, audit, system = normal["process"], normal["audit"], normal["system"]
    console = normal["console"]

    def add(name, ok, note=""):
        out.append((name, ok, note))

    # C1 -- the three files, exactly these names, in the input-3 folder
    names = sorted(p.name for p in (folder / "gateway").glob("*.log"))
    add("C1", names == ["audit.log", "process.log", "system.log"], f"found {names}")

    # C2 -- every line parses, and has the five required fields
    every = proc + audit + system
    required = {"ts", "trace_id", "stream", "service", "event"}
    missing = [r["event"] for r in every if not required <= set(r)]
    add("C2", not missing, f"{len(every)} records" if not missing else f"missing on {missing[:3]}")

    # C3 -- one run, one id; two runs, two ids
    ids = {r["trace_id"] for r in proc if r["trace_id"]}
    other = {r["trace_id"] for r in records(data["folders"]["debug"], "process") if r["trace_id"]}
    add("C3", len(ids) == 1 and HEX32.match(next(iter(ids))) and not (ids & other),
        f"run id {next(iter(ids), '?')[:12]}…, distinct from the debug run's")

    # C4 -- a health check produces no trace_id-bearing line
    health = [r for r in audit if r["event"] == "inbound_request" and r.get("route") == "/healthz"]
    add("C4", bool(health) and all(not r["trace_id"] for r in health),
        f"{len(health)} health record(s), trace_id empty")

    # C5 -- many services: the id crosses. Not exercised by this harness.
    add("C5", None, "not run — needs gateway and email_agent live end to end")

    # C6 -- step_in/step_out pairing, a raise, and the iterated tally
    ins = [r["step"] for r in proc if r["event"] == "step_in"]
    outs = [r["step"] for r in proc if r["event"] == "step_out"]
    raised = [r for r in proc if r["event"] == "step_out" and r["step"] == "write_back"]
    iterated = [r for r in proc if r["event"] == "step_out" and r["step"] == "compose"]
    tally = iterated[0]["outputs"] if iterated else {}
    add("C6", sorted(ins) == sorted(outs)
        and bool(raised) and raised[0]["status"] == "failed" and raised[0].get("reason")
        and tally.get("total") == 3 and tally.get("failed_keys") == ["540690773311"]
        and tally.get("skipped_keys") == ["540690771102"],
        f"{len(ins)} in / {len(outs)} out; compose tally {tally.get('total')}/"
        f"ok {tally.get('ok')} failed {tally.get('failed')} skipped {tally.get('skipped')}")

    # C7 -- a model call's request.prompt, cut in normal, whole in debug
    def model_call(mode):
        return next(r for r in data[mode]["audit"]
                    if r["event"] == "outbound_call" and r["kind"] == "model")
    n_prompt = model_call("normal")["request"]["prompt"]
    d_prompt = model_call("debug")["request"]["prompt"]
    add("C7", d_prompt == PROMPT and n_prompt.startswith(PROMPT[:240])
        and n_prompt.endswith("chars)") and len(n_prompt) < len(d_prompt),
        f"normal {len(n_prompt)} chars, debug {len(d_prompt)} = the prompt sent")

    # C8 -- api_key redacted at depth 5, in debug too; the keepers untouched
    def deep(mode):
        r = next(x for x in data[mode]["process"]
                 if x["event"] == "step_out" and x["step"] == "redaction_probe")
        return r["outputs"]["config"]["a"]["b"]["c"]["d"]
    add("C8", all(deep(m)["api_key"] == "<redacted>" for m in ("normal", "debug"))
        and deep("normal")["credential_name"] == "ANTHROPIC_API_KEY"
        and deep("normal")["max_tokens"] == 2000,
        "api_key <redacted> at depth 5 in normal and debug; credential_name, max_tokens kept")

    # C9 -- unwritable folder: log_write_failed once, run completes, stdout
    add("C9", data["unwritable"]["said_once"] and data["unwritable"]["completed"],
        data["unwritable"]["note"])

    # C10 -- service_start carries version; credential_resolved name and source
    start = [r for r in system if r["event"] == "service_start"]
    creds = [r for r in system if r["event"] == "credential_resolved"]
    blob = json.dumps(system)
    add("C10", bool(start) and start[0].get("version") == "0.1.0"
        and len(creds) == 2 and all(c.get("credential_name") and c.get("source") for c in creds)
        and "sk-ant" not in blob,
        f"version={start[0].get('version') if start else '?'}, "
        f"{len(creds)} credential_resolved, no secret value anywhere")

    # C11 -- three modes differ, in the files and on the console
    sizes = {m: sum((data["folders"][m] / "gateway" / f"{s}.log").stat().st_size
                    for s in ("process", "audit")) for m in MODES}
    t_in = next(r for r in data["terse"]["process"] if r["event"] == "step_in")
    n_in = next(r for r in data["normal"]["process"] if r["event"] == "step_in")
    d_in = next(r for r in data["debug"]["process"] if r["event"] == "step_in")
    t_req = model_call("terse")["request"]
    consoles = {data[m]["console"] for m in MODES}
    add("C11", sizes["terse"] < sizes["normal"] < sizes["debug"]
        and not t_in["inputs"] and n_in["inputs"] and d_in["inputs"]
        and set(t_req) == {"keys"} and len(consoles) == 3,
        f"bytes terse {sizes['terse']} < normal {sizes['normal']} < debug {sizes['debug']}; "
        "three different consoles")

    # C12 -- the console rendering
    lines = [ln for ln in console.splitlines() if ln.strip()]
    #: §1.1's continuation lines (a model call's `prompt:` / `response:`) are
    #: indented to the content column and carry no time, stream or trace id.
    started = [ln for ln in lines
               if re.match(r"^\d\d:\d\d:\d\d\s+(system|process|audit)\s", ln)]
    in_run = [ln for ln in started if not ln.rstrip().endswith("trace_id=")]
    proc_lines = [ln for ln in started if re.match(r"^\S+\s+process\s", ln)]
    tid = next(iter(ids), "")
    add("C12", bool(in_run)
        and all(ln.rstrip().endswith(f"trace_id={tid}") for ln in in_run)
        and len(started) + len([l for l in lines if l.startswith(" ")]) == len(lines)
        and all(" trace_id=" in ln for ln in started)
        and any(" >  IN " in ln for ln in lines) and any(" <  OUT " in ln for ln in lines)
        and any(" -> CALL " in ln for ln in lines)
        and any("+ ok" in ln for ln in lines) and any("x failed" in ln for ln in lines)
        and any("! skipped" in ln for ln in lines)
        and any("compose 1/3" in ln for ln in lines)
        and "prompt:" in console and "response:" in console
        and " run_start " in proc_lines[0] and " run_end " in proc_lines[-1]
        and "uvicorn" not in console,
        f"{len(lines)} lines ({len(started)} records, {len(lines) - len(started)} continuation); "
        f"run_start first and run_end last on process; every record line ends trace_id=")

    # C13 -- every outbound_call inside a step carries that step
    inside = [r for r in audit if r["event"] == "outbound_call"]
    add("C13", all(r.get("step") in ("read_leads", "compose") for r in inside),
        f"{len(inside)} calls, each naming its step")

    # C14 -- kind and operation on every call, operation survives terse
    ops = {(r["kind"], r["operation"]) for r in inside}
    t_calls = [r for r in data["terse"]["audit"] if r["event"] == "outbound_call"]
    add("C14", all(r.get("kind") and r.get("operation") for r in inside)
        and len({k for k, _ in ops}) >= 3
        and all(r.get("operation") for r in t_calls),
        f"kinds {sorted({k for k, _ in ops})}; operation present in terse too")

    return out


def unwritable_run() -> dict:
    """C9 -- a folder that cannot be written: one log_write_failed, the work
    still finishes, the records go to standard output."""
    blocked = Path(tempfile.mkdtemp()) / "no-entry"
    blocked.mkdir()
    blocked.chmod(0o500)
    env = {**os.environ, "LOG_DIR": str(blocked / "logs"), "LOG_MODE": "normal",
           "NO_COLOR": "1", "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, __file__, "--emit"], env=env,
                          capture_output=True, text=True, timeout=60)
    blocked.chmod(0o700)
    said = done.stdout.count('"event": "log_write_failed"')
    return {"said_once": said == 1,
            "completed": '"event": "run_end"' in done.stdout and done.returncode == 0,
            "note": f"log_write_failed x{said}, run_end present, "
                    f"{len(done.stdout.splitlines())} lines on standard output"}


def main() -> int:
    if "--emit" in sys.argv:
        workload()
        return 0

    data: dict = {"folders": {}}
    root = Path(tempfile.mkdtemp(prefix="logcheck-"))
    for mode in MODES:
        folder = root / mode
        console = produce(mode, folder)
        data["folders"][mode] = folder
        data[mode] = {"console": console,
                      "process": records(folder, "process"),
                      "audit": records(folder, "audit"),
                      "system": records(folder, "system")}
    data["unwritable"] = unwritable_run()

    results = run_checks(data)
    width = max(len(n) for n, _, _ in results)
    for name, ok, note in results:
        mark = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        print(f"{name:<{width}}  {mark:<4}  {note}")

    if "--evidence" in sys.argv:
        print("\n" + "=" * 78 + "\nC2 — service_start (system.log), then run_start (process.log)\n")
        print(json.dumps(next(r for r in data["normal"]["system"]
                              if r["event"] == "service_start"))[:400])
        print(json.dumps(next(r for r in data["normal"]["process"]
                              if r["event"] == "run_start"))[:400])
        print("\n" + "=" * 78 + "\nC14 — three CALL lines, one run's console\n")
        for line in data["normal"]["console"].splitlines():
            if " -> CALL " in line:
                print(line)
        print("\n" + "=" * 78 + "\nC6 — the failed step_out, debug then normal\n")
        for mode in ("debug", "normal"):
            rec = next(r for r in data[mode]["process"]
                       if r["event"] == "step_out" and r["step"] == "write_back")
            has = "traceback" in rec
            print(f"{mode:<7} traceback={'present, ' + str(len(rec.get('traceback', ''))) + ' chars' if has else 'absent'}"
                  f"  reason={rec.get('reason')}")
        print("\n" + "=" * 78 + "\nC11 — the same model call in all three modes\n")
        for mode in MODES:
            rec = next(r for r in data[mode]["audit"]
                       if r["event"] == "outbound_call" and r["kind"] == "model")
            print(f"--- {mode} ---")
            print("request= " + json.dumps(rec["request"])[:300])
            print("response=" + json.dumps(rec["response"])[:300])
        print("\n" + "=" * 78 + "\nC12 — one full run's console, normal mode\n")
        print(data["normal"]["console"])

    return 0 if all(ok is not False for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
