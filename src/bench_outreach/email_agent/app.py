"""The Email Agent's HTTP surface — the gateway's hand-off target.

    POST /email/trigger   {trigger_id, object_id, ...}   <- NEXT_AGENT_URL
    GET  /healthz         process is up
    GET  /readyz          503 unless it could actually work a lead

Run:  uvicorn bench_outreach.email_agent.app:app --port 8081
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..common.hubspot_mcp import HubSpotMCPError
from ..common.settings import REPO_ROOT, env, env_bool
from ..common.trace import ARROW, Trace, banner, new_trace_id
from .agent import (ARTIFACTS, EmailAgent, Outcome, check_sender_identity,
                    check_skill_files, load_config)
from .sender import DryRunSender, MailgunSender

CONFIG_PATH = REPO_ROOT / "config" / "email.yaml"
TRIGGER_PATH = "/email/trigger"
LOG_DIR = REPO_ROOT / "logs" / "email_agent"


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

        # The gateway's trigger id IS the trace id, so both services' lines join on
        # it. A direct POST (no gateway) mints its own rather than inventing a shape.
        t = Trace("email_agent", trigger_id or new_trace_id(),
                  f"Gateway {ARROW} Email Agent   lead {object_id}", LOG_DIR)
        try:
            outcome = await run_in_threadpool(agent.work, object_id, trigger_id, t)
        except HubSpotMCPError as exc:
            # HubSpot unreachable is not this lead's fault — 503 so the gateway retries.
            t.fail(f"HubSpot: {exc}")
            t.end("FAILED 503 — cannot reach HubSpot, gateway should retry")
            return JSONResponse(status_code=503, content={"error": f"hubspot: {exc}"})
        t.end(_verdict(outcome), **outcome.as_dict())
        return JSONResponse(status_code=200, content=outcome.as_dict())

    banner("Email Agent", [
        f"listening for the gateway at  {TRIGGER_PATH}",
        "mode  " + ("DRY RUN — writes drafts to a file, sends nothing" if dry_run
                    else "*** LIVE — REAL EMAILS WILL BE SENT ***"),
        f"model  {config['model']['name']}",
        f"skill  {config['skill']['path']}",
        "log file  logs/email_agent/email_agent.jsonl",
    ] + ([f"PROBLEM: {p}" for p in problems] or ["config  ok"]))
    _log_startup(agent, config, dry_run, problems)
    return app


def _log_startup(agent: EmailAgent | None, config: dict[str, Any], dry_run: bool,
                 problems: list[str]) -> None:
    """One SYSTEM line at boot naming the skills this process offers the model."""
    catalogue = agent.catalogue if agent else []
    Trace("email_agent", new_trace_id(), "Email Agent starting", LOG_DIR).system(
        "skill_loaded", f"{len(catalogue)} skill(s) in the catalogue: "
        + ", ".join(s.name for s in catalogue),
        dry_run=dry_run, model=config["model"]["name"],
        agent_md=agent.system_prompt.path.relative_to(REPO_ROOT).as_posix() if agent else "",
        agent_md_sha=agent.system_prompt.sha if agent else "",
        skills=[f"{s.path.relative_to(REPO_ROOT).as_posix()}@{s.sha}" for s in catalogue],
        problems=problems)


def _verdict(outcome: Outcome) -> str:
    return {
        "sent": ("EMAIL WRITTEN — dry run, nothing was emailed, HubSpot unchanged"
                 if outcome.dry_run else "EMAIL SENT to this lead"),
        "skipped": f"SKIPPED — {outcome.reason}",
        "failed": f"FAILED — {outcome.reason}",
    }.get(outcome.status, outcome.status)


app = create_app()
