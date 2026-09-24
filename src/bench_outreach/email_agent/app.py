"""The Email Agent's HTTP surface — the gateway's hand-off target.

    POST /email/trigger   {trigger_id, object_id, ...}   <- NEXT_AGENT_URL
    GET  /healthz         process is up
    GET  /readyz          503 unless it could actually work a lead

Run:  uvicorn bench_outreach.email_agent.app:app --port 8081
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import __version__
from ..common.hubspot_mcp import HubSpotMCPError
from ..common.settings import REPO_ROOT, env, env_bool
from . import email_agent_logging as elog
from .agent import (ARTIFACTS, EmailAgent, Outcome, check_sender_identity,
                    check_skill_files, load_config)
from .sender import DryRunSender, MailgunSender

CONFIG_PATH = REPO_ROOT / "config" / "email.yaml"
TRIGGER_PATH = "/email/trigger"


def build_agent(config: dict[str, Any], dry_run: bool) -> tuple[EmailAgent | None, list[str]]:
    """Choose a sender, check the identity, build the agent (which reads the skill
    catalogue). Problems are returned, not raised: the service starts, explains
    itself on /readyz, and refuses work — better than a stack trace at boot.
    """
    problems = check_skill_files(REPO_ROOT / str(config["skill"]["path"]))
    if not dry_run:
        missing = check_sender_identity(config["sender"])
        if missing:
            problems.append("sender identity incomplete: " + ", ".join(missing))
    if not env("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is not set")
    sender = (DryRunSender(ARTIFACTS / f"drafts-{date.today().isoformat()}.md")
              if dry_run else MailgunSender())
    try:
        agent = EmailAgent(sender=sender, config=config, dry_run=dry_run)
    except ValueError as exc:                       # no skills, or one without a description
        problems.append(str(exc))
        agent = None
    return agent, problems


def create_app(config: dict[str, Any] | None = None, agent: EmailAgent | None = None,
               dry_run: bool | None = None) -> FastAPI:
    config = config or load_config(CONFIG_PATH)
    if dry_run is None:
        dry_run = not env_bool("EMAIL_AGENT_SEND")
    problems: list[str] = []
    if agent is None:
        agent, problems = build_agent(config, dry_run)
    ready = agent is not None and not problems

    app = FastAPI(title="Bench Outreach — Email Agent", version="0.1.0")
    app.state.agent = agent
    app.state.dry_run = dry_run

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "dry_run": dry_run}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        body: dict[str, Any] = {"status": "ready" if ready else "not-ready",
                                "dry_run": dry_run, "model": config["model"]["name"]}
        if problems:
            body["problems"] = problems
        return JSONResponse(status_code=200 if ready else 503, content=body)

    @app.post(TRIGGER_PATH)
    async def trigger(request: Request) -> JSONResponse:
        payload = await request.json()
        object_id = str(payload.get("object_id") or "").strip()
        trigger_id = str(payload.get("trigger_id") or "")
        if not object_id:
            return JSONResponse(status_code=400, content={"error": "object_id is required"})
        if agent is None:
            return JSONResponse(status_code=503,
                                content={"error": "agent not configured", "problems": problems})

        #: §3 -- the id crosses from the gateway in the call's metadata. Missing
        #: or malformed, a new one is born here and `trace_missing` says so.
        adopted = elog.from_traceparent(request.headers.get("traceparent"))
        started = time.perf_counter()
        with elog.run(f"POST {TRIGGER_PATH}", adopt=adopted,
                      object_id=object_id, trigger_id=trigger_id):
            if not adopted:
                elog.system("trace_missing", where=f"POST {TRIGGER_PATH}")
            try:
                outcome = await run_in_threadpool(agent.work, object_id, trigger_id)
            except HubSpotMCPError as exc:
                # HubSpot unreachable is not this lead's fault — 503 so the gateway retries.
                elog.finish(status="failed", reason=f"hubspot: {exc}")
                elog.inbound_request(route=TRIGGER_PATH, method="POST", status=503, ok=False,
                                     duration_ms=(time.perf_counter() - started) * 1000)
                return JSONResponse(status_code=503, content={"error": f"hubspot: {exc}"})
            elog.finish(**outcome.as_dict())
            elog.inbound_request(route=TRIGGER_PATH, method="POST", status=200, ok=True,
                                 duration_ms=(time.perf_counter() - started) * 1000)
            return JSONResponse(status_code=200, content=outcome.as_dict())

    #: §1 and §6 -- everything in Part 1 is set up by this one call.
    elog.configure_logging(
        folder=str(REPO_ROOT / "logs" / "email_agent"), version=__version__,
        credentials={"ANTHROPIC_API_KEY": "env" if env("ANTHROPIC_API_KEY") else "unset",
                     "MAILGUN_API_KEY": "env" if env("MAILGUN_API_KEY") else "unset",
                     "HUBSPOT_MCP_TOKEN_FILE": "file"},
        dependencies={"anthropic": "ok" if env("ANTHROPIC_API_KEY") else "no key",
                      "mailgun": "ok" if dry_run or env("MAILGUN_API_KEY") else "no key"})
    catalogue = agent.catalogue if agent else []
    elog.system("skills_loaded", count=len(catalogue),
                skills=[f"{s.path.relative_to(REPO_ROOT).as_posix()}@{s.sha}" for s in catalogue],
                model=config["model"]["name"], dry_run=dry_run,
                agent_md=agent.system_prompt.path.relative_to(REPO_ROOT).as_posix() if agent else "",
                agent_md_sha=agent.system_prompt.sha if agent else "")
    for problem in problems:
        elog.system("config_invalid", setting="startup", given="", used=problem)
    return app


def _verdict(outcome: Outcome) -> str:
    return {
        "sent": ("EMAIL WRITTEN — dry run, nothing was emailed, HubSpot unchanged"
                 if outcome.dry_run else "EMAIL SENT to this lead"),
        "skipped": f"SKIPPED — {outcome.reason}",
        "failed": f"FAILED — {outcome.reason}",
    }.get(outcome.status, outcome.status)


app = create_app()
