"""HubSpot MCP client, exercised over the real MCP protocol against an in-process
fake server (the SDK's MCPServer + ASGI transport). No network, no HubSpot."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from bench_outreach.common.hubspot_mcp import (FileTokenStorage, HubSpotMCP, HubSpotMCPError,
                                               NotAuthorised)

logging.getLogger("mcp").setLevel(logging.WARNING)


# ------------------------------------------------------------ fake HubSpot MCP
def fake_hubspot() -> tuple[MCPServer, list]:
    srv = MCPServer("fake-hubspot")
    calls: list = []

    @srv.tool()
    def get_crm_objects(objectType: str, objectIds: list[int], properties: list[str] | None = None) -> dict:
        calls.append(("get", objectIds))
        return {"objects": [{"id": objectIds[0], "properties": {"email": "ann@example.com",
                                                                "firstname": "Ann"}}]}

    @srv.tool()
    def manage_crm_objects(confirmationStatus: str, updateRequest: dict) -> dict:
        if confirmationStatus != "CONFIRMED":
            raise ValueError("write requires confirmationStatus=CONFIRMED")
        calls.append(("update", updateRequest))
        return {"updateResults": {"summary": {"updated": 1, "failed": 0}}}

    return srv, calls


class InProcessMCP(HubSpotMCP):
    """Same client; HTTP goes to the fake server's ASGI app instead of the network."""

    def __init__(self, app, **kw):
        super().__init__(server_url="http://127.0.0.1/mcp", **kw)
        self._app = app

    def _http_client(self, auth):
        return httpx2.AsyncClient(auth=auth, timeout=self._timeout,
                                  transport=httpx2.ASGITransport(app=self._app))


@pytest.fixture
def authorised_storage(tmp_path: Path) -> FileTokenStorage:
    st = FileTokenStorage(tmp_path / "tok.json")
    asyncio.run(st.set_client_info(OAuthClientInformationFull(
        client_id="cid", redirect_uris=["http://localhost:8765/callback"])))
    asyncio.run(st.set_tokens(OAuthToken(access_token="tok", token_type="Bearer",
                                         expires_in=3600, refresh_token="r")))
    return st


def run_against_fake(coro_factory):
    """Start the fake server's lifespan, run the coroutine, stop it."""
    srv, calls = fake_hubspot()
    app = srv.streamable_http_app(
        json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def _run():
        async with app.router.lifespan_context(app):
            try:
                return await coro_factory(app)
            except HubSpotMCPError as exc:
                return exc          # carried out of the server's task group unwrapped
    outcome = asyncio.run(_run())
    if isinstance(outcome, HubSpotMCPError):
        raise outcome
    return outcome, calls


# -------------------------------------------------------------------- tests
def test_lists_tools_over_the_protocol(authorised_storage):
    tools, _ = run_against_fake(lambda app: InProcessMCP(app, storage=authorised_storage).alist_tools())
    assert set(tools) == {"get_crm_objects", "manage_crm_objects"}


def test_reads_a_contact(authorised_storage):
    result, calls = run_against_fake(lambda app: InProcessMCP(app, storage=authorised_storage).acall(
        "get_crm_objects", {"objectType": "CONTACT", "objectIds": [777], "properties": ["email"]}))
    assert result["objects"][0]["properties"]["email"] == "ann@example.com"
    assert calls == [("get", [777])]


def test_writes_a_contact(authorised_storage):
    args = {"confirmationStatus": "CONFIRMED", "updateRequest": {"objects": [
        {"objectType": "contacts", "objectId": 777, "properties": {"email_status": "SENT"}}]}}
    result, calls = run_against_fake(
        lambda app: InProcessMCP(app, storage=authorised_storage).acall("manage_crm_objects", args))
    assert result["updateResults"]["summary"] == {"updated": 1, "failed": 0}
    assert calls[0][1]["objects"][0]["properties"] == {"email_status": "SENT"}


def test_tool_error_becomes_hubspot_mcp_error(authorised_storage):
    with pytest.raises(HubSpotMCPError, match="manage_crm_objects"):
        run_against_fake(lambda app: InProcessMCP(app, storage=authorised_storage).acall(
            "manage_crm_objects", {"confirmationStatus": "NO", "updateRequest": {}}))


def test_headless_without_tokens_raises_not_authorised(tmp_path, monkeypatch):
    """No tokens + no interactive handlers → a clear instruction, never a hung browser prompt."""
    monkeypatch.setenv("HUBSPOT_MCP_CLIENT_ID", "cid")
    st = FileTokenStorage(tmp_path / "empty.json")
    client = HubSpotMCP(storage=st)
    # What the provider does on a 401 with no tokens: call the redirect handler.
    # Headless, that must raise a clear instruction instead of hanging on a browser.
    with pytest.raises(NotAuthorised, match="hubspot_auth.py"):
        asyncio.run(client._redirect("https://app.hubspot.com/oauth/authorize?x=y"))


def test_expired_access_token_is_blanked_so_the_sdk_refreshes(tmp_path):
    """After a restart the SDK cannot tell a stored access token has aged out; it would
    send it, get 401, and demand a fresh browser consent instead of using the refresh
    token. Blanking the expired access token puts it on the refresh path."""
    st = FileTokenStorage(tmp_path / "t.json")
    asyncio.run(st.set_tokens(OAuthToken(access_token="stale", token_type="Bearer",
                                         expires_in=1800, refresh_token="r")))
    import json as _json
    data = _json.loads((tmp_path / "t.json").read_text())
    data["expires_at"] = 0                       # pretend it aged out
    (tmp_path / "t.json").write_text(_json.dumps(data))

    token = asyncio.run(st.get_tokens())
    assert token.access_token == "", "an expired access token must not look usable"
    assert token.refresh_token == "r", "the refresh token must survive, or we re-consent"


def test_unexpired_access_token_is_returned_as_is(tmp_path):
    st = FileTokenStorage(tmp_path / "t.json")
    asyncio.run(st.set_tokens(OAuthToken(access_token="fresh", token_type="Bearer",
                                         expires_in=1800, refresh_token="r")))
    assert asyncio.run(st.get_tokens()).access_token == "fresh"


def test_storage_round_trip_and_client_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBSPOT_MCP_CLIENT_ID", "abc")
    monkeypatch.setenv("HUBSPOT_MCP_CLIENT_SECRET", "s3")
    monkeypatch.setenv("HUBSPOT_MCP_REDIRECT_URI", "http://localhost:9999/cb")
    st = FileTokenStorage(tmp_path / "t.json")
    assert not st.has_tokens()
    info = asyncio.run(st.get_client_info())
    assert info.client_id == "abc" and str(info.redirect_uris[0]) == "http://localhost:9999/cb"
    assert info.token_endpoint_auth_method == "client_secret_post"
    asyncio.run(st.set_tokens(OAuthToken(access_token="a", token_type="Bearer", refresh_token="r")))
    assert st.has_tokens()
    again = asyncio.run(st.get_tokens())
    assert again.refresh_token == "r"
    assert "tokens" in (tmp_path / "t.json").read_text()
