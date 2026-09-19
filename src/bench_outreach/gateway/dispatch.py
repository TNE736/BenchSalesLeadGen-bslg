"""Hand a decision to its target agent — or, with no agent configured yet, to the outbox."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from ..common.settings import env
from .router import Decision, Target


@dataclass
class Outcome:
    trigger_id: str
    ok: bool
    destination: str
    status_code: int | None
    attempts: int
    latency_ms: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Dispatcher:
    def __init__(
        self,
        targets: dict[str, Target],
        outbox_path: Path,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._targets = targets
        self._outbox = outbox_path
        self._client = client or httpx.Client()
        self._sleep = sleep

    def url_for(self, target_key: str) -> str:
        return env(self._targets[target_key].url_env)

    def send(self, decision: Decision) -> Outcome:
        target = self._targets[decision.target]
        url = self.url_for(decision.target)
        payload = decision.handoff_payload()
        if not url:
            return self._to_outbox(decision, payload)

        started = time.perf_counter()
        attempts = 0
        error: str | None = None
        status: int | None = None
        while attempts <= target.max_retries:
            attempts += 1
            try:
                resp = self._client.post(url, json=payload, timeout=target.timeout_seconds)
                status = resp.status_code
                if 200 <= status < 300:
                    return Outcome(decision.trigger_id, True, url, status, attempts,
                                   (time.perf_counter() - started) * 1000)
                error = f"HTTP {status}"
                if status < 500 and status != 429:
                    break                                   # a 4xx will not fix itself
            except httpx.TimeoutException as exc:
                # NOT retried. A timeout means "no answer", not "not done" — the agent
                # may be mid-compose and about to email this person. Retrying that
                # ambiguity is how one lead receives two emails; it happened on
                # 17 Sep 2026 with a 10s timeout against a 17s compose.
                error = (f"no answer within {target.timeout_seconds}s — the agent may "
                         f"still be working this lead ({type(exc).__name__})")
                break
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
            if attempts <= target.max_retries:
                self._sleep(target.backoff_seconds * (2 ** (attempts - 1)))
        return Outcome(decision.trigger_id, False, url, status, attempts,
                       (time.perf_counter() - started) * 1000, error)

    def _to_outbox(self, decision: Decision, payload: dict[str, Any]) -> Outcome:
        """No agent yet: keep the decision on disk so nothing is lost."""
        self._outbox.parent.mkdir(parents=True, exist_ok=True)
        record = {"recorded_at": int(time.time() * 1000), **payload}
        with self._outbox.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return Outcome(decision.trigger_id, True, f"outbox:{self._outbox.name}", None, 1, 0.0)
