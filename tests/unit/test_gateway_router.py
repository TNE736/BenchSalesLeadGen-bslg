from pathlib import Path

import pytest

from bench_outreach.gateway.events import EnvelopeError, HubSpotEvent, parse_batch
from bench_outreach.gateway.router import (DedupeStore, Route, Router, RoutingConfig,
                                           Target, mint_trigger_id)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config() -> RoutingConfig:
    return RoutingConfig(
        routes=[Route(id="dm", property="decision_maker", values=("Yes", "true"),
                      target="next", subscription_type="contact.propertyChange")],
        targets={"next": Target(key="next", url_env="NEXT_AGENT_URL")},
    )


def _event(**over) -> dict:
    base = {"eventId": 1, "objectId": 101, "portalId": 247408852,
            "subscriptionType": "contact.propertyChange", "attemptNumber": 0,
            "occurredAt": 1700000000000, "propertyName": "decision_maker",
            "propertyValue": "Yes", "changeSource": "CRM_UI"}
    base.update(over)
    return base


def test_shipped_config_loads_and_targets_decision_maker():
    cfg = RoutingConfig.load(REPO_ROOT / "config" / "gateway.yaml")
    assert [r.property for r in cfg.routes] == ["decision_maker"]
    assert cfg.routes[0].target in cfg.targets


def test_yes_routes_case_insensitively():
    router = Router(_config())
    res = router.route(parse_batch([_event(propertyValue="yes"), _event(eventId=2, propertyValue="TRUE")]))
    assert len(res.decisions) == 2 and not res.discards


def test_no_is_discarded_with_reason():
    res = Router(_config()).route(parse_batch([_event(propertyValue="No")]))
    assert not res.decisions
    assert res.discards[0].reason == "not a routing condition"


def test_other_property_is_discarded():
    res = Router(_config()).route(parse_batch([_event(propertyName="seniority", propertyValue="Yes")]))
    assert not res.decisions and len(res.discards) == 1


def test_decision_carries_object_id_and_stable_trigger_id():
    res = Router(_config()).route(parse_batch([_event()]))
    d = res.decisions[0]
    assert d.handoff_payload()["object_id"] == "101"
    assert d.trigger_id == mint_trigger_id(HubSpotEvent.from_payload(_event()))
    assert d.trigger_id.startswith("trg-")


def test_missing_object_id_is_a_routing_error_not_a_discard():
    ev = _event()
    del ev["objectId"]
    res = Router(_config()).route(parse_batch([ev]))
    assert not res.decisions and not res.discards and len(res.errors) == 1


def test_redelivery_of_handled_event_is_dropped_but_first_delivery_is_not():
    router = Router(_config())
    first = router.route(parse_batch([_event(eventId=7)]))
    assert len(first.decisions) == 1
    router.dedupe.remember("7")
    again = router.route(parse_batch([_event(eventId=7, attemptNumber=1)]))
    assert not again.decisions and again.discards[0].reason.startswith("duplicate")
    # attemptNumber=0 with the same id is treated as a fresh event (HubSpot semantics)
    fresh = router.route(parse_batch([_event(eventId=7, attemptNumber=0)]))
    assert len(fresh.decisions) == 1


def test_same_event_twice_in_one_batch_routes_once():
    res = Router(_config()).route(parse_batch([_event(eventId=9), _event(eventId=9)]))
    assert len(res.decisions) == 1 and len(res.discards) == 1


def test_dedupe_store_expires_and_caps():
    store = DedupeStore(ttl_seconds=0, max_entries=2)
    store.remember("a")
    assert not store.seen("a")            # ttl 0 → expired immediately
    store = DedupeStore(ttl_seconds=100, max_entries=2)
    for k in "abc":
        store.remember(k)
    assert len(store) == 2 and not store.seen("a")


@pytest.mark.parametrize("payload,status", [
    ({"not": "a list"}, 200),   # single object is normalised, not rejected
    ("string", 400), ([], 400), ([1], 400), ([{}] * 101, 413),
])
def test_envelope_validation(payload, status):
    if status == 200:
        assert len(parse_batch(payload)) == 1
    else:
        with pytest.raises(EnvelopeError) as exc:
            parse_batch(payload)
        assert exc.value.status_code == status
