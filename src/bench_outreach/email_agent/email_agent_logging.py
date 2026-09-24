"""Logging Spec v3.0 for the Email Agent — this service's one logging module.

Everything in Part 1 of the spec is set up by `configure_logging()`. Import
what you need from here, not from anywhere else: the service name is bound
once, in this file, and every record this service writes carries it.

    from .email_agent_logging import configure_logging, log, step, run

The spec is implemented in `common/logging_core`, shared with the other
service in this repo rather than copied — see the report.
"""

from __future__ import annotations

from typing import Any

from ..common import logging_core as _core
from ..common.logging_core import (AUDIT, DEBUG, NORMAL, PROCESS, REDACTED,  # noqa: F401
                                   SYSTEM, TERSE, Logger, Step, cut, from_traceparent,
                                   new_trace_id, redact, trace_id, traceparent,
                                   valid_trace_id)

SERVICE = "email_agent"

_LOGGER: Logger | None = None


def configure_logging(*, folder: str | None = None, mode: str | None = None,
                      version: str = "0.1.0", credentials: dict[str, str] | None = None,
                      dependencies: dict[str, Any] | None = None) -> Logger:
    """Set up this service's logging. Called once, as the service starts."""
    global _LOGGER
    _LOGGER = _core.configure_logging(SERVICE, folder=folder, mode=mode, version=version,
                                      credentials=credentials, dependencies=dependencies)
    return _LOGGER


def log() -> Logger:
    """This service's logger. Configured on first use if the service did not."""
    if _LOGGER is None:
        return configure_logging()
    return _LOGGER


def reset() -> None:
    """For the test suite, which moves the folder between tests."""
    global _LOGGER
    _LOGGER = None


def run(trigger: str, *, adopt: str = "", **inputs: Any):
    return log().run(trigger, adopt=adopt, **inputs)


def step(name: str, **inputs: Any):
    return log().step(name, **inputs)


def item(number: int, of: int, **key: Any):
    return log().item(number, of, **key)


def finish(**outputs: Any) -> None:
    log().finish(**outputs)


def emit(stream: str, event: str, **fields: Any) -> None:
    log().emit(stream, event, **fields)


def system(event: str, **fields: Any) -> None:
    log().system(event, **fields)


def outbound_call(**fields: Any) -> None:
    _core.outbound_call(log(), **fields)


def inbound_request(**fields: Any) -> None:
    _core.inbound_request(log(), **fields)


def mode() -> str:
    return log().mode
