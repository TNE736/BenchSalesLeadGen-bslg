"""The HubSpot webhook envelope: a JSON array of up to 100 event objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_BATCH = 100


class EnvelopeError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class HubSpotEvent:
    event_id: str | None
    object_id: str | None
    portal_id: str | None
    subscription_type: str | None
    property_name: str | None
    property_value: str | None
    attempt_number: int
    occurred_at: int | None
    change_source: str | None

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> "HubSpotEvent":
        def text(key: str) -> str | None:
            value = raw.get(key)
            return None if value is None else str(value)

        try:
            attempt = int(raw.get("attemptNumber") or 0)
        except (TypeError, ValueError):
            attempt = 0
        try:
            occurred = int(raw["occurredAt"]) if raw.get("occurredAt") is not None else None
        except (TypeError, ValueError):
            occurred = None

        return cls(
            event_id=text("eventId"),
            object_id=text("objectId"),
            portal_id=text("portalId"),
            subscription_type=text("subscriptionType"),
            property_name=text("propertyName"),
            property_value=text("propertyValue"),
            attempt_number=attempt,
            occurred_at=occurred,
            change_source=text("changeSource"),
        )

    @property
    def is_redelivery(self) -> bool:
        return self.attempt_number > 0


def parse_batch(payload: Any) -> list[HubSpotEvent]:
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise EnvelopeError("body must be a JSON array of events")
    if not payload:
        raise EnvelopeError("body contains no events")
    if len(payload) > MAX_BATCH:
        raise EnvelopeError(f"batch of {len(payload)} exceeds {MAX_BATCH}", status_code=413)
    events = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise EnvelopeError(f"event at index {index} is not an object")
        events.append(HubSpotEvent.from_payload(item))
    return events
