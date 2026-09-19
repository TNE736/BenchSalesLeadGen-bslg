"""FastAPI ingress.  Run:  uvicorn bench_outreach.gateway.app:app --port 8080

    POST /hubspot/events   the HubSpot webhook — the only entry point
    GET  /healthz          process is up
    GET  /readyz           config can actually verify and hand off (503 if not)

Response codes are HubSpot's control signal:
    200  every event reached a terminal outcome (handed off, or discarded on purpose)
    401  signature missing / stale / wrong
    400  / 413  malformed envelope / over 100 events
    503  something matched but was not handed off — HubSpot must redeliver
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..common.settings import REPO_ROOT, env, env_bool
from ..common.trace import ARROW, INFO, SYSTEM, Trace, banner, redact
from .dispatch import Dispatcher
from .events import EnvelopeError, parse_batch
from .router import Router, RoutingConfig
from .signature import SignatureError, verify_v3

CONFIG_PATH = REPO_ROOT / "config" / "gateway.yaml"
INGRESS_PATH = "/hubspot/events"


# ------------------------------------------------------------------ logging
def _logger(log_dir: Path) -> logging.Logger:
    """One JSON object per line, to stdout and to logs/gateway/gateway.jsonl."""
    log = logging.getLogger("bench_outreach.gateway")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter("%(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file = logging.FileHandler(log_dir / "gateway.jsonl", encoding="utf-8")
        file.setFormatter(fmt)
        log.addHandler(file)
    except OSError:
        pass
    return log


def _emit(log: logging.Logger, event: str, **fields: Any) -> None:
    """A SYSTEM record: about the service, not about any one lead.

    Carries `service` and `stream` like every Trace record, so one reader can make
    sense of both files. Without them these lines were the only ones in either log
    that no filter could select.
    """
    log.info(json.dumps({"ts": int(time.time() * 1000), "service": "gateway",
                         "stream": SYSTEM, "event": event, **redact(fields)},
                        default=str))


# --------------------------------------------------------------------- app
def create_app(
    config: RoutingConfig | None = None,
    dispatcher: Dispatcher | None = None,
    log_dir: Path | None = None,
) -> FastAPI:
    config = config or RoutingConfig.load(CONFIG_PATH)
    log_dir = log_dir or (REPO_ROOT / "logs" / "gateway")
    log = _logger(log_dir)
    router = Router(config)
    dispatcher = dispatcher or Dispatcher(config.targets, log_dir / "outbox.jsonl")

    skip_signature = env_bool("GATEWAY_SKIP_SIGNATURE")      # local dev only

    def config_problems() -> dict[str, str]:
        problems: dict[str, str] = {}
        if not skip_signature and not env("HUBSPOT_APP_CLIENT_SECRET"):
            problems["HUBSPOT_APP_CLIENT_SECRET"] = "unset — every webhook will be rejected 401"
        if not skip_signature and not env("GATEWAY_PUBLIC_URL"):
            problems["GATEWAY_PUBLIC_URL"] = "unset — behind a tunnel the signed URI will not match"
        return problems

    app = FastAPI(title="Bench Outreach — Agent Gateway", version="0.1.0")
    app.state.router = router
    app.state.dispatcher = dispatcher

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Response:
        problems = config_problems()
        targets = {k: (dispatcher.url_for(k) or "outbox") for k in config.targets}
        body = {"status": "ready" if not problems else "not-ready", "targets": targets}
        if problems:
            body["config"] = problems
        return JSONResponse(status_code=200 if not problems else 503, content=body)

    def process(raw: bytes, method: str, uri: str, headers: dict[str, str],
                run_id: str, source_ip: str) -> tuple[int, dict[str, Any]]:
        with Trace("gateway", run_id, "HubSpot webhook received", log_dir) as t:
            # ---- 1. did this really come from HubSpot? -----------------------
            t.step(f"HubSpot {ARROW} Gateway   from {source_ip}")
            t.detail(f"addressed to {uri}")
            if skip_signature:
                t.warn("signature check DISABLED (GATEWAY_SKIP_SIGNATURE) — local dev only")
            else:
                try:
                    verify_v3(
                        secret=env("HUBSPOT_APP_CLIENT_SECRET"), method=method, uri=uri,
                        body=raw.decode("utf-8", "replace"),
                        timestamp=headers.get("x-hubspot-request-timestamp", ""),
                        signature=headers.get("x-hubspot-signature-v3", ""),
                    )
                    t.ok("signature verified — this is genuinely from HubSpot")
                except SignatureError as exc:
                    t.fail(f"signature rejected: {exc}")
                    t.end("REJECTED 401 — nothing was routed", status=401)
                    return 401, {"run_id": run_id, "error": "unauthorized"}

            # ---- 2. is the envelope well formed? ----------------------------
            try:
                events = parse_batch(json.loads(raw or b""))
            except json.JSONDecodeError:
                t.fail("body is not valid JSON")
                t.end("REJECTED 400", status=400)
                return 400, {"run_id": run_id, "error": "invalid JSON body"}
            except EnvelopeError as exc:
                t.fail(str(exc))
                t.end(f"REJECTED {exc.status_code}", status=exc.status_code)
                return exc.status_code, {"run_id": run_id, "error": str(exc)}
            t.ok(f"{len(events)} event(s) in this delivery")

            # ---- 3. which of them do we act on? -----------------------------
            t.step("Deciding what to do with each event")
            result = router.route(events)
            for d in result.discards:
                t.detail(f"contact {d.event.object_id}  {d.event.property_name}="
                         f'"{d.event.property_value}"  {ARROW} IGNORED: {d.reason}',
                         object_id=d.event.object_id, reason=d.reason)
            for err in result.errors:
                t.fail(err)
            for d in result.decisions:
                t.detail(f"contact {d.event.object_id}  {d.event.property_name}="
                         f'"{d.event.property_value}"  {ARROW} matched route '
                         f'"{d.route_id}", sending to {d.target}',
                         object_id=d.event.object_id, route_id=d.route_id)
                t.detail(f"trigger id {d.trigger_id} minted for this lead",
                         trigger_id=d.trigger_id)

            # ---- 4. hand each one to the agent ------------------------------
            outcomes = []
            for decision in result.decisions:
                url = dispatcher.url_for(decision.target) or "(no agent — outbox file)"
                t.step(f"Gateway {ARROW} Email Agent   POST {url}")
                t.detail(f"sending only: contact id {decision.event.object_id} "
                         f"+ trigger {decision.trigger_id} (no personal data)")
                router.dedupe.remember(decision.event.event_id)   # reserve before handing off
                outcome = dispatcher.send(decision)
                if outcome.ok:
                    t.ok(f"agent accepted it — HTTP {outcome.status_code} in "
                         f"{outcome.latency_ms / 1000:.1f}s (attempt {outcome.attempts})",
                         **outcome.as_dict())
                else:
                    router.dedupe.forget(decision.event.event_id)  # let HubSpot retry
                    t.fail(f"hand-off failed after {outcome.attempts} attempt(s): "
                           f"{outcome.error}", **outcome.as_dict())
                outcomes.append(outcome)

            failed = [o for o in outcomes if not o.ok]
            summary = {
                "run_id": run_id, "received": len(events), "routed": len(result.decisions),
                "discarded": len(result.discards), "handed_off": len(outcomes) - len(failed),
                "failed": len(failed), "routing_errors": result.errors,
                "handoffs": [o.as_dict() for o in outcomes],
            }
            status = 503 if failed or result.errors else 200
            verdict = (f"{len(events)} received {INFO} {len(result.decisions)} routed {INFO} "
                       f"{len(result.discards)} ignored {INFO} "
                       f"{len(outcomes) - len(failed)} handed off")
            if status == 503:
                verdict += f" {INFO} {len(failed)} FAILED — answering 503 so HubSpot retries"
            t.end(verdict, **{k: v for k, v in summary.items() if k != "handoffs"})
            return status, summary

    @app.post(INGRESS_PATH)
    async def hubspot_events(request: Request) -> Response:
        run_id = uuid.uuid4().hex[:12]
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        source_ip = request.client.host if request.client else "unknown"
        status, body = await run_in_threadpool(
            process, raw, request.method, _signed_uri(request), headers, run_id, source_ip)
        return JSONResponse(status_code=status, content=body)

    problems = config_problems()
    banner("Agent Gateway", [
        f"listening for HubSpot at  {INGRESS_PATH}",
        f"public URL (what HubSpot signs)  {env('GATEWAY_PUBLIC_URL') or '(not set)'}",
        f"routes  " + ", ".join(f'{r.property}={list(r.values)} {ARROW} {r.target}'
                                for r in config.routes),
        "hands off to  " + ", ".join(
            f"{k} {ARROW} {dispatcher.url_for(k) or '(no agent — outbox file)'}"
            for k in config.targets),
        f"signature check  {'DISABLED (local dev)' if skip_signature else 'on'}",
        f"log file  logs/gateway/gateway.jsonl",
    ] + ([f"PROBLEM: {k}: {v}" for k, v in problems.items()] or ["config  ok"]))
    _emit(log, "startup", ingress=INGRESS_PATH, routes=[r.id for r in config.routes],
          config_problems=problems)
    return app


def _signed_uri(request: Request) -> str:
    """The URL HubSpot hashed. A tunnel rewrites Host, so pin it with GATEWAY_PUBLIC_URL."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    public = env("GATEWAY_PUBLIC_URL").rstrip("/")
    if public:
        return f"{public}{path}"
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto.split(',')[0].strip()}://{host.split(',')[0].strip()}{path}"
    return str(request.url)


app = create_app()
