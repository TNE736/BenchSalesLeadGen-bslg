"""The HubSpot boundary (Input 1): the trace id travels as data, on the contact.

HubSpot fires a webhook and carries no headers of ours back, so §5.8 applies:
the service before the boundary writes the trace id into a named field, and
the service after it adopts from that field. Here both are the gateway -- the
first delivery writes; a redelivery reads -- and the email agent reads the
same field in the contact read it already makes.

Best effort throughout. A carrier the gateway could not read or write is a
logged, visible gap (`outbound_call` with `ok: false`); it never stops a lead.
"""

from __future__ import annotations

import time
from typing import Any

from ..common.hubspot_mcp import HubSpotMCP, HubSpotMCPError, attempt_listener
from . import gateway_logging as glog

#: The carrier field, named for this boundary in Input 1. Its name must not
#: contain a SECRET_SUBSTRING (§5.8), or the id would be redacted from the
#: record that carries it -- `trace_id` contains none.
FIELD = "trace_id"
BOUNDARY = "hubspot.contact.trace_id"
#: The NAME of the credential the MCP client calls with (§5). Never a value.
CREDENTIAL = "HUBSPOT_MCP_TOKEN_FILE"


class HubSpotCarrier:
    def __init__(self, hubspot: Any = None) -> None:
        self._hs = hubspot

    @property
    def hs(self) -> Any:
        if self._hs is None:
            self._hs = HubSpotMCP()        # cheap: no network until the first call
        return self._hs

    def read(self, object_id: str) -> str:
        """The id the contact already carries, or `""`. Invalid values are `""`
        too: a carrier that holds junk is a carrier that holds nothing."""
        params = {"objectIds": [int(object_id)], "properties": [FIELD]}
        result = self._call("get_crm_objects", params,
                            lambda: self.hs.call("get_crm_objects", {"objectType": "CONTACT", **params}))
        if result is None:
            return ""
        props = ((result.get("objects") or [{}])[0].get("properties")) or {}
        value = str(props.get(FIELD) or "").strip()
        return value if glog.valid_trace_id(value) else ""

    def write(self, object_id: str, trace_id: str) -> bool:
        """§5.8, the writer: the write that triggers the next hop carries the
        current trace id. Overwrites any earlier value -- the contact carries
        the id of the latest flow that touched it."""
        params = {"objectId": int(object_id), FIELD: trace_id}
        result = self._call("manage_crm_objects", params, lambda: self.hs.call(
            "manage_crm_objects", {
                "confirmationStatus": "CONFIRMED",
                "updateRequest": {"objects": [{
                    "objectType": "contacts", "objectId": int(object_id),
                    "properties": {FIELD: trace_id}}]}}))
        return result is not None

    @staticmethod
    def _call(endpoint: str, params: dict[str, Any], call: Any) -> Any:
        """One MCP call, one audit record per attempt (§5), never raising.
        Returns the result, or None when it failed."""
        started = time.perf_counter()
        common = dict(peer="hubspot", kind="mcp", operation=endpoint,
                      endpoint="https://mcp.hubspot.com", method="tools/call",
                      credential_name=CREDENTIAL, request=dict(params))
        tries = {"n": 0}

        def failed_attempt(attempt: int, error: str, duration_ms: float) -> None:
            tries["n"] = attempt
            glog.outbound_call(ok=False, status=None, error=error, attempt=attempt,
                               duration_ms=duration_ms, response={}, **common)

        token = attempt_listener.set(failed_attempt)
        try:
            result = call()
        except (HubSpotMCPError, ValueError, OSError) as exc:
            if tries["n"] == 0:
                glog.outbound_call(ok=False, status=None, error=str(exc), attempt=1,
                                   duration_ms=_ms(started), response={}, **common)
            return None
        finally:
            attempt_listener.reset(token)
        glog.outbound_call(ok=True, status=200, attempt=tries["n"] + 1,
                           duration_ms=_ms(started),
                           response={"keys": sorted(map(str, result or {}))}, **common)
        return result


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
