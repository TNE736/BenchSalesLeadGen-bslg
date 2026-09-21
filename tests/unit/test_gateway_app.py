"""End-to-end through the HTTP layer with a fake agent — no network, no HubSpot."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bench_outreach.gateway.app import create_app
from bench_outreach.gateway.dispatch import Dispatcher
from bench_outreach.gateway.router import Route, RoutingConfig, Target
from bench_outreach.gateway.signature import compute_v3

SECRET = "test-secret"
PUBLIC = "https://gw.example.test"
URI = PUBLIC + "/hubspot/events"


def _config() -> RoutingConfig:
    return RoutingConfig(
        routes=[Route(id="dm", property="decision_maker", values=("Yes",),
                      target="next", subscription_type="contact.propertyChange")],
        targets={"next": Target(key="next", url_env="NEXT_AGENT_URL", max_retries=1,
                                backoff_seconds=0)},
    )


class FakeAgent:
    """Stands in for the next agent: records what it received, answers as told."""

    def __init__(self, status=200):
        self.status = status
        self.received = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.received.append(json.loads(request.content))
        return httpx.Response(self.status)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUBSPOT_APP_CLIENT_SECRET", SECRET)
    monkeypatch.setenv("GATEWAY_PUBLIC_URL", PUBLIC)
    monkeypatch.delenv("GATEWAY_SKIP_SIGNATURE", raising=False)
    monkeypatch.setenv("NEXT_AGENT_URL", "http://agent.test/trigger")
    return tmp_path


def _client(env, agent: FakeAgent | None):
    transport = httpx.MockTransport(agent.handler) if agent else None
    dispatcher = Dispatcher(_config().targets, env / "outbox.jsonl",
                            client=httpx.Client(transport=transport) if transport else None,
                            sleep=lambda _: None)
    return TestClient(create_app(_config(), dispatcher, log_dir=env))


def _signed(body: list, ts: str | None = None, sig: str | None = None):
    raw = json.dumps(body)
    ts = ts or str(int(time.time() * 1000))
    return raw, {"X-HubSpot-Signature-v3": sig or compute_v3(SECRET, "POST", URI, raw, ts),
                 "X-HubSpot-Request-Timestamp": ts, "Content-Type": "application/json"}


def _event(**over):
    base = {"eventId": 11, "objectId": 555, "portalId": 247408852,
            "subscriptionType": "contact.propertyChange", "attemptNumber": 0,
            "occurredAt": int(time.time() * 1000), "propertyName": "decision_maker",
            "propertyValue": "Yes", "changeSource": "CRM_UI"}
    base.update(over)
    return base


def test_yes_is_handed_off_with_trigger_and_contact_id(env):
    agent = FakeAgent()
    raw, headers = _signed([_event()])
    r = _client(env, agent).post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["routed"] == 1 and body["handed_off"] == 1
    assert agent.received[0]["object_id"] == "555"
    assert agent.received[0]["trigger_id"].startswith("bslg-")


def test_no_is_discarded_and_still_200(env):
    agent = FakeAgent()
    raw, headers = _signed([_event(propertyValue="No")])
    r = _client(env, agent).post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["discarded"] == 1 and not agent.received


def test_bad_signature_is_401_and_nothing_is_dispatched(env):
    agent = FakeAgent()
    raw, headers = _signed([_event()], sig="AAAA")
    r = _client(env, agent).post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 401 and not agent.received


def test_agent_failure_is_503_so_hubspot_retries(env):
    agent = FakeAgent(status=500)
    raw, headers = _signed([_event()])
    r = _client(env, agent).post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 503
    assert r.json()["failed"] == 1 and len(agent.received) == 2   # 1 try + 1 retry


def test_timeout_is_not_retried_because_the_agent_may_be_mid_compose(env):
    """The real bug from 17 Sep 2026: a 10s timeout against a 17s compose made the
    gateway retry, and the email agent wrote a SECOND email for the same person.
    A timeout means 'no answer', not 'not done' — it must never be retried."""
    calls: list[int] = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("too slow", request=request)

    dispatcher = Dispatcher(_config().targets, env / "outbox.jsonl",
                            client=httpx.Client(transport=httpx.MockTransport(slow)),
                            sleep=lambda _: None)
    client = TestClient(create_app(_config(), dispatcher, log_dir=env))
    raw, headers = _signed([_event()])
    r = client.post("/hubspot/events", content=raw, headers=headers)

    assert len(calls) == 1, "a timeout must be tried exactly once, never retried"
    assert r.status_code == 503, "and reported as unresolved so nothing pretends it worked"
    assert "may still be working" in r.json()["handoffs"][0]["error"]


def test_connection_error_is_still_retried(env):
    """A refused connection is unambiguous — the agent never saw it — so retry is safe."""
    calls: list[int] = []

    def refused(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("nothing listening", request=request)

    dispatcher = Dispatcher(_config().targets, env / "outbox.jsonl",
                            client=httpx.Client(transport=httpx.MockTransport(refused)),
                            sleep=lambda _: None)
    client = TestClient(create_app(_config(), dispatcher, log_dir=env))
    raw, headers = _signed([_event()])
    client.post("/hubspot/events", content=raw, headers=headers)
    assert len(calls) == 2, "1 try + 1 retry, per max_retries in the test config"


def test_shipped_timeout_exceeds_the_measured_compose_time():
    """Guards the config value itself: composing took 8-17s live."""
    from pathlib import Path as _P
    cfg = RoutingConfig.load(_P(__file__).resolve().parents[2] / "config" / "gateway.yaml")
    assert cfg.targets["next_agent"].timeout_seconds >= 60


def test_redelivery_after_success_is_not_dispatched_twice(env):
    agent = FakeAgent()
    client = _client(env, agent)
    raw, headers = _signed([_event(eventId=42)])
    assert client.post("/hubspot/events", content=raw, headers=headers).status_code == 200
    raw, headers = _signed([_event(eventId=42, attemptNumber=1)])
    r = client.post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["discarded"] == 1
    assert len(agent.received) == 1


def test_no_agent_configured_goes_to_outbox_not_lost(env, monkeypatch):
    monkeypatch.setenv("NEXT_AGENT_URL", "")
    raw, headers = _signed([_event()])
    r = _client(env, None).post("/hubspot/events", content=raw, headers=headers)
    assert r.status_code == 200 and r.json()["handed_off"] == 1
    lines = (env / "outbox.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["object_id"] == "555"


def test_readyz_reports_missing_secret(env, monkeypatch):
    monkeypatch.setenv("HUBSPOT_APP_CLIENT_SECRET", "")
    r = _client(env, FakeAgent()).get("/readyz")
    assert r.status_code == 503 and "HUBSPOT_APP_CLIENT_SECRET" in r.json()["config"]


def test_readyz_ok_when_configured(env):
    r = _client(env, FakeAgent()).get("/readyz")
    assert r.status_code == 200 and r.json()["targets"]["next"] == "http://agent.test/trigger"
