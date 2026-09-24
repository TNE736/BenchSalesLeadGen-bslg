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
import time
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import __version__
from ..common.settings import REPO_ROOT, env, env_bool
from . import gateway_logging as glog
from .carrier import HubSpotCarrier
from .dispatch import Dispatcher
from .events import EnvelopeError, parse_batch
from .router import Decision, Router, RoutingConfig
from .signature import SignatureError, verify_v3

CONFIG_PATH = REPO_ROOT / "config" / "gateway.yaml"
#: The outbox is an ARTIFACT, not a log: a decision parked for later because no agent
#: URL was configured. It is work waiting to be done, and it carries a contact id, so
#: it belongs beside the email agent's drafts and transcripts rather than among the
#: three machine-readable streams. logs/ holds those three files and nothing else.
ARTIFACTS = REPO_ROOT / "artifacts" / "gateway"
INGRESS_PATH = "/hubspot/events"
#: The route on the email agent this gateway calls -- the `operation` of the
#: hand-off in `audit.log` (§5).
TRIGGER_ROUTE = "/email/trigger"


# --------------------------------------------------------------------- app
def create_app(
    config: RoutingConfig | None = None,
    dispatcher: Dispatcher | None = None,
    hubspot: Any = None,
) -> FastAPI:
    config = config or RoutingConfig.load(CONFIG_PATH)
    carrier = HubSpotCarrier(hubspot)
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
                source_ip: str) -> tuple[int, dict[str, Any], list[Decision], str]:
        """One webhook delivery. Input 1: the work starts here, so the trace id
        is born here -- HubSpot's webhook carries none and the gateway does not
        read HubSpot before deciding.

        It returns that id, because §3 says background work copies the id in
        WHEN IT IS SCHEDULED. Reading it from the store after this function has
        returned is one line too late: `run` restores the previous value on the
        way out, so the caller would read `""` and the background work would
        mint an id of its own -- which is exactly what happened on the
        07:37:58 run before this was fixed.
        """
        with glog.run(f"POST {INGRESS_PATH}", source_ip=source_ip, uri=uri,
                      bytes=len(raw)) as current:
            with glog.step("verify_and_parse", source_ip=source_ip, uri=uri) as frame:
                if skip_signature:
                    glog.emit(glog.PROCESS, "signature_check_skipped",
                              reason="GATEWAY_SKIP_SIGNATURE set — local dev only")
                else:
                    try:
                        verify_v3(
                            secret=env("HUBSPOT_APP_CLIENT_SECRET"), method=method, uri=uri,
                            body=raw.decode("utf-8", "replace"),
                            timestamp=headers.get("x-hubspot-request-timestamp", ""),
                            signature=headers.get("x-hubspot-signature-v3", ""),
                        )
                        glog.emit(glog.AUDIT, "signature_verified", source_ip=source_ip)
                    except SignatureError as exc:
                        glog.emit(glog.AUDIT, "signature_rejected", source_ip=source_ip,
                                  reason=str(exc))
                        frame.failed(f"SignatureError: {exc}")
                        glog.finish(status=401, reason="unauthorized")
                        return 401, {"error": "unauthorized"}, [], current

                try:
                    events = parse_batch(json.loads(raw or b""))
                except json.JSONDecodeError:
                    frame.failed("JSONDecodeError: body is not valid JSON")
                    glog.finish(status=400)
                    return 400, {"error": "invalid JSON body"}, [], current
                except EnvelopeError as exc:
                    frame.failed(f"EnvelopeError: {exc}")
                    glog.finish(status=exc.status_code)
                    return exc.status_code, {"error": str(exc)}, [], current
                frame.ok(events=len(events))

            with glog.step("route_events", events=len(events)) as frame:
                result = router.route(events)
                for d in result.discards:
                    glog.emit(glog.PROCESS, "event_discarded", object_id=d.event.object_id,
                              property=d.event.property_name, value=d.event.property_value,
                              reason=d.reason)
                for err in result.errors:
                    glog.emit(glog.PROCESS, "event_unusable", reason=err)
                for d in result.decisions:
                    glog.emit(glog.PROCESS, "event_routed", object_id=d.event.object_id,
                              property=d.event.property_name, value=d.event.property_value,
                              route_id=d.route_id, target=d.target, trigger_id=d.trigger_id)
                frame.ok(routed=len(result.decisions), discarded=len(result.discards),
                         unusable=len(result.errors))

            with glog.step("schedule_work", leads=len(result.decisions)) as frame:
                #: HubSpot allows five seconds and the hand-off takes a minute, so
                #: the answer goes first and the work continues in the background
                #: under this same trace id.
                for decision in result.decisions:
                    router.dedupe.remember(decision.event.event_id)   # claim first, work second
                    glog.emit(glog.PROCESS, "work_scheduled",
                              object_id=decision.event.object_id,
                              trigger_id=decision.trigger_id)
                frame.ok(scheduled=len(result.decisions))

            summary = {
                "received": len(events), "routed": len(result.decisions),
                "discarded": len(result.discards), "scheduled": len(result.decisions),
                "routing_errors": result.errors,
            }
            status = 503 if result.errors else 200
            glog.finish(**summary, status=status)
            return status, summary, list(result.decisions), current

    def work(decision: Decision, trace_id: str) -> None:
        """One routed lead, in the background, after HubSpot has its answer.

        §3: the id was copied in when this was scheduled and is set here -- the
        scheduling scope has already ended by the time this runs.

        Input 1, the crossing into HubSpot: every new campaign gets the id born
        at the door, written onto the contact, overwriting whatever an earlier
        campaign left there. Nothing is read back: a HubSpot redelivery is
        dropped by the dedupe store before it reaches this function, so there is
        no adopt path to build.
        """
        oid = decision.event.object_id
        with glog.run(f"background lead {oid}", adopt=trace_id,
                      object_id=oid, trigger_id=decision.trigger_id,
                      attempt=decision.event.attempt_number):
            with glog.step("write_trace_id", object_id=oid) as frame:
                if carrier.write(oid, glog.trace_id()):
                    frame.ok(written=True, field="trace_id")
                else:
                    frame.skipped("HubSpot refused the write", written=False)

            url = dispatcher.url_for(decision.target) or "(no agent — outbox file)"
            with glog.step("hand_off", object_id=oid, trigger_id=decision.trigger_id,
                           target=decision.target, url=url) as frame:
                #: One audit record per ATTEMPT (§5), written as each happens.
                outcome = dispatcher.send(decision, on_attempt=lambda a: glog.outbound_call(
                    peer="email_agent", kind="service",
                    operation=f"POST {TRIGGER_ROUTE}", endpoint=url, method="POST",
                    status=a.status_code, ok=a.ok, attempt=a.attempt,
                    duration_ms=a.latency_ms, error=a.error or "",
                    request={"object_id": oid, "trigger_id": decision.trigger_id},
                    response={"status": a.status_code}))
                if outcome.ok:
                    frame.ok(status=outcome.status_code, attempts=outcome.attempts)
                    glog.finish(status="handed_off", attempts=outcome.attempts)
                else:
                    router.dedupe.forget(decision.event.event_id)   # a re-fire may retry it
                    frame.failed(outcome.error or "hand-off failed",
                                 attempts=outcome.attempts)
                    glog.finish(status="hand_off_failed", attempts=outcome.attempts)

    @app.post(INGRESS_PATH)
    async def hubspot_events(request: Request) -> Response:
        raw = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        source_ip = request.client.host if request.client else "unknown"
        started = time.perf_counter()
        tasks = BackgroundTasks()
        #: `process` opens the run, so the id is born inside the threadpool call
        #: and comes back as its fourth return value -- captured while the store
        #: still held it, which is what §3 asks for.
        status, body, decisions, trace_id = await run_in_threadpool(
            process, raw, request.method, _signed_uri(request), headers, source_ip)
        for decision in decisions:
            tasks.add_task(work, decision, trace_id)
        glog.inbound_request(route=INGRESS_PATH, method=request.method, status=status,
                             ok=status < 500,
                             duration_ms=(time.perf_counter() - started) * 1000)
        return JSONResponse(status_code=status, content=body, background=tasks)

    problems = config_problems()
    #: §1 and §6 -- everything in Part 1 is set up by this one call, at the
    #: service's start, before any run.
    glog.configure_logging(
        folder=str(REPO_ROOT / "logs" / "gateway"), version=__version__,
        credentials={"HUBSPOT_APP_CLIENT_SECRET":
                     "env" if env("HUBSPOT_APP_CLIENT_SECRET") else "unset",
                     "NEXT_AGENT_URL": "env" if env("NEXT_AGENT_URL") else "unset"},
        dependencies={f"target:{key}": "ok" if dispatcher.url_for(key)
                      else "no URL configured — hand-offs go to the outbox file"
                      for key in config.targets})
    if problems:
        for name, why in problems.items():
            glog.system("config_invalid", setting=name, given="", used=why)
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
