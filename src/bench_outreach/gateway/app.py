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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..common.settings import REPO_ROOT, env, env_bool
from ..common.trace import ARROW, AUDIT, INFO, Trace, banner, new_trace_id
from .dispatch import Dispatcher
from .events import EnvelopeError, parse_batch
from .router import Router, RoutingConfig
from .signature import SignatureError, verify_v3

CONFIG_PATH = REPO_ROOT / "config" / "gateway.yaml"
#: The outbox is an ARTIFACT, not a log: a decision parked for later because no agent
#: URL was configured. It is work waiting to be done, and it carries a contact id, so
#: it belongs beside the email agent's drafts and transcripts rather than among the
#: three machine-readable streams. logs/ holds those three files and nothing else.
ARTIFACTS = REPO_ROOT / "artifacts" / "gateway"
INGRESS_PATH = "/hubspot/events"


# --------------------------------------------------------------------- app
def create_app(
    config: RoutingConfig | None = None,
    dispatcher: Dispatcher | None = None,
    log_dir: Path | None = None,
) -> FastAPI:
    config = config or RoutingConfig.load(CONFIG_PATH)
    log_dir = log_dir or (REPO_ROOT / "logs" / "gateway")
    router = Router(config)
    dispatcher = dispatcher or Dispatcher(config.targets, ARTIFACTS / "outbox.jsonl")

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
                trace_id: str, source_ip: str) -> tuple[int, dict[str, Any]]:
        with Trace("gateway", trace_id, "HubSpot webhook received", log_dir) as t:
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
                    t.event("signature_verified", stream=AUDIT, source_ip=source_ip)
                except SignatureError as exc:
                    t.fail(f"signature rejected: {exc}")
                    t.event("signature_rejected", stream=AUDIT, source_ip=source_ip,
                            reason=str(exc))
                    t.end("REJECTED 401 — nothing was routed", status=401)
                    return 401, {"error": "unauthorized"}

            # ---- 2. is the envelope well formed? ----------------------------
            try:
                events = parse_batch(json.loads(raw or b""))
            except json.JSONDecodeError:
                t.fail("body is not valid JSON")
                t.end("REJECTED 400", status=400)
                return 400, {"error": "invalid JSON body"}
            except EnvelopeError as exc:
                t.fail(str(exc))
                t.end(f"REJECTED {exc.status_code}", status=exc.status_code)
                return exc.status_code, {"error": str(exc)}
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
                # The same shape every other hop uses, so "show me every external call
                # this trace made" is one query across both services. It is also what
                # makes a hand-off timeout legible: HubSpot gives us 5 seconds and the
                # agent can take 20, and that used to be a line of prose.
                t.outbound_call(service="email_agent", endpoint=url, ok=outcome.ok,
                                status=outcome.status_code, duration_ms=outcome.latency_ms,
                                params={"trigger_id": decision.trigger_id,
                                        "object_id": decision.event.object_id,
                                        "attempts": outcome.attempts},
                                error=outcome.error or "")
                if outcome.ok:
                    t.ok(f"agent accepted it — HTTP {outcome.status_code} in "
                         f"{outcome.latency_ms / 1000:.1f}s (attempt {outcome.attempts})")
                else:
                    router.dedupe.forget(decision.event.event_id)  # let HubSpot retry
                    t.fail(f"hand-off failed after {outcome.attempts} attempt(s): "
                           f"{outcome.error}")
                outcomes.append(outcome)

            failed = [o for o in outcomes if not o.ok]
            summary = {
                "received": len(events), "routed": len(result.decisions),
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
        raw = await request.body()
        # The id of this DELIVERY, not of the work it asks for. Two deliveries of one
        # event are two HTTP requests and get two ids -- the timestamp alone differs,
        # and that is the point: the signature, the source IP and the verdict belong
        # to the request that carried them. The work is joined by the trigger id the
        # router mints per event (`mint_trigger_id`), which the email agent adopts.
        trace_id = new_trace_id(raw.decode("utf-8", "replace") if raw else "")
        headers = {k.lower(): v for k, v in request.headers.items()}
        source_ip = request.client.host if request.client else "unknown"
        status, body = await run_in_threadpool(
            process, raw, request.method, _signed_uri(request), headers, trace_id, source_ip)
        return JSONResponse(status_code=status, content=body)

    problems = config_problems()
    banner("Agent Gateway", [
        f"listening for HubSpot at  {INGRESS_PATH}",
        f"public URL (what HubSpot signs)  {env('GATEWAY_PUBLIC_URL') or '(not set)'}",
        "routes  " + ", ".join(f'{r.property}={list(r.values)} {ARROW} {r.target}'
                                for r in config.routes),
        "hands off to  " + ", ".join(
            f"{k} {ARROW} {dispatcher.url_for(k) or '(no agent — outbox file)'}"
            for k in config.targets),
        f"signature check  {'DISABLED (local dev)' if skip_signature else 'on'}",
        "log file  logs/gateway/{system,process,audit}.jsonl",
    ] + ([f"PROBLEM: {k}: {v}" for k, v in problems.items()] or ["config  ok"]))
    # Through Trace, like the email agent, so it lands in logs/gateway/system.jsonl.
    # It used to go through a second logger of the gateway's own into gateway.jsonl:
    # the record was tagged stream=system and then written to the one file no stream
    # filter could reach.
    Trace("gateway", new_trace_id(), "Agent Gateway starting", log_dir).system(
        "startup", f"{len(config.routes)} route(s): "
        + ", ".join(r.id for r in config.routes),
        ingress=INGRESS_PATH, routes=[r.id for r in config.routes],
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
