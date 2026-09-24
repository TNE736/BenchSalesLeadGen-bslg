"""Decide, per event: route it, or discard it with a reason."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ..common.settings import env
from .events import HubSpotEvent


@dataclass(frozen=True)
class Route:
    id: str
    property: str
    values: tuple[str, ...]
    target: str
    subscription_type: str | None = None

    def matches(self, event: HubSpotEvent) -> bool:
        if self.subscription_type and event.subscription_type != self.subscription_type:
            return False
        if (event.property_name or "").lower() != self.property.lower():
            return False
        value = (event.property_value or "").strip().lower()
        return value in {v.lower() for v in self.values}


@dataclass(frozen=True)
class Target:
    key: str
    url_env: str
    timeout_seconds: int = 10
    max_retries: int = 2
    backoff_seconds: float = 0.5


@dataclass
class RoutingConfig:
    routes: list[Route]
    targets: dict[str, Target]
    dedupe_ttl_seconds: int = 86400
    dedupe_max_entries: int = 10000

    @classmethod
    def load(cls, path: Path) -> "RoutingConfig":
        doc: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        targets = {
            key: Target(key=key, **{k: v for k, v in (spec or {}).items()})
            for key, spec in (doc.get("targets") or {}).items()
        }
        routes = [
            Route(
                id=str(r["id"]),
                property=str(r["property"]),
                values=tuple(str(v) for v in r.get("values", [])),
                target=str(r["target"]),
                subscription_type=r.get("subscription_type"),
            )
            for r in (doc.get("routes") or [])
        ]
        for route in routes:
            if route.target not in targets:
                raise ValueError(f"route {route.id} points at unknown target {route.target}")
            if not route.values:
                raise ValueError(f"route {route.id} has no values — it would never match")
        dedupe = doc.get("dedupe") or {}
        return cls(
            routes=routes,
            targets=targets,
            dedupe_ttl_seconds=int(dedupe.get("ttl_seconds", 86400)),
            dedupe_max_entries=int(dedupe.get("max_entries", 10000)),
        )


class DedupeStore:
    """TTL + LRU set of handled event ids. In-memory: one process, one store."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if now - stamp <= self._ttl:
                break
            self._seen.popitem(last=False)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)

    def seen(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        with self._lock:
            self._purge(time.time())
            return event_id in self._seen

    def remember(self, event_id: str | None) -> None:
        if not event_id:
            return
        with self._lock:
            self._seen[event_id] = time.time()
            self._seen.move_to_end(event_id)
            self._purge(time.time())

    def forget(self, event_id: str | None) -> None:
        if not event_id:
            return
        with self._lock:
            self._seen.pop(event_id, None)

    def __len__(self) -> int:
        return len(self._seen)


@dataclass(frozen=True)
class Decision:
    trigger_id: str
    route_id: str
    target: str
    event: HubSpotEvent

    def handoff_payload(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "route_id": self.route_id,
            "object_id": self.event.object_id,
            "property": self.event.property_name,
            "value": self.event.property_value,
            "occurred_at": self.event.occurred_at,
        }


@dataclass(frozen=True)
class Discard:
    reason: str
    event: HubSpotEvent


@dataclass
class RoutingResult:
    decisions: list[Decision] = field(default_factory=list)
    discards: list[Discard] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)   # matched but unusable → 503


#: Prefix on every trigger id. One project, one prefix, so a line pulled out of
#: a shared log still says which system produced it.
PROJECT_ID = env("BENCH_PROJECT_ID") or "bslg"


def mint_trigger_id(event: HubSpotEvent) -> str:
    """The id of the WORK this event asks for: `<project>-<utc stamp>-<12 hex>`.

    This is a TRIGGER id, not a trace id, and the two must never be confused.
    A trace id is random, 32 hex, born where the work starts (§3). A trigger id
    is the opposite on purpose: DETERMINISTIC per HubSpot event -- seeded from
    the event's identity, stamped from its own `occurred_at` -- so a redelivery
    mints the same string and the dedupe store recognises it. Never 32 hex, so
    no parser can mistake one for the other. Logged as an ordinary field.
    """
    key = f"{event.portal_id}:{event.event_id}" if event.event_id else (
        f"{event.portal_id}:{event.object_id}:{event.property_name}:"
        f"{event.property_value}:{event.occurred_at}"
    )
    when = (datetime.fromtimestamp(event.occurred_at / 1000, timezone.utc)
            if event.occurred_at else datetime.now(timezone.utc))
    stamp = when.strftime("%Y%m%dT%H%M%S")
    return f"{PROJECT_ID}-{stamp}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


class Router:
    def __init__(self, config: RoutingConfig, dedupe: DedupeStore | None = None) -> None:
        self.config = config
        self.dedupe = dedupe or DedupeStore(config.dedupe_ttl_seconds, config.dedupe_max_entries)

    def route(self, events: list[HubSpotEvent]) -> RoutingResult:
        result = RoutingResult()
        seen_in_batch: set[str] = set()
        for event in events:
            route = next((r for r in self.config.routes if r.matches(event)), None)
            if route is None:
                result.discards.append(Discard("not a routing condition", event))
                continue
            if event.event_id and (
                event.event_id in seen_in_batch
                or (event.is_redelivery and self.dedupe.seen(event.event_id))
            ):
                result.discards.append(Discard("duplicate delivery already handled", event))
                continue
            if not event.object_id:
                result.errors.append(
                    f"event {event.event_id} matched {route.id} but carries no objectId")
                continue
            if event.event_id:
                seen_in_batch.add(event.event_id)
            result.decisions.append(Decision(
                trigger_id=mint_trigger_id(event), route_id=route.id,
                target=route.target, event=event))
        return result
