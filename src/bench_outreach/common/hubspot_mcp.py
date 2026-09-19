"""HubSpot through HubSpot's own hosted MCP server (https://mcp.hubspot.com).

The only module in the repo that knows how to reach HubSpot. Every agent does:

    hs = HubSpotMCP()
    contact = hs.call("get_crm_objects", {"objectType": "CONTACT", "objectIds": [777],
                                          "properties": ["email", "firstname"]})
    hs.call("manage_crm_objects", {"confirmationStatus": "CONFIRMED",
                                   "updateRequest": {"objects": [{"objectType": "contacts",
                                       "objectId": 777, "properties": {"email_status": "SENT"}}]}})

Auth is OAuth 2.1 + PKCE against the MCP connector registered in HubSpot
(Development → MCP Connectors). The SDK discovers HubSpot's endpoints, does the
PKCE dance and refreshes tokens; we only persist them. `scripts/hubspot_auth.py`
runs the one-time browser consent; after that every run is headless.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx2
from mcp import ClientSession
from mcp.shared.exceptions import MCPError

try:  # Python < 3.11
    BaseExceptionGroup  # noqa: B018
except NameError:  # pragma: no cover
    from exceptiongroup import BaseExceptionGroup  # type: ignore
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.oauth2 import (build_oauth_authorization_server_metadata_discovery_urls,
                                    create_oauth_metadata_request,
                                    handle_auth_metadata_response)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (AuthorizationCodeResult, OAuthClientInformationFull,
                             OAuthClientMetadata, OAuthMetadata, OAuthToken)

from .settings import REPO_ROOT, env

DEFAULT_SERVER_URL = "https://mcp.hubspot.com"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"
DEFAULT_TOKEN_FILE = REPO_ROOT / ".hubspot_mcp_tokens.json"     # git-ignored


class HubSpotMCPError(RuntimeError):
    """A tool call failed; the message names the tool and HubSpot's reason."""


class NotAuthorised(HubSpotMCPError):
    """No stored tokens. Run `python scripts/hubspot_auth.py` once."""


# ------------------------------------------------------------- token storage
class FileTokenStorage(TokenStorage):
    """Tokens + client info in one JSON file. Client id/secret come from `.env`
    (the MCP connector HubSpot issued); tokens are written after the consent."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(env("HUBSPOT_MCP_TOKEN_FILE") or path or DEFAULT_TOKEN_FILE)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        """Stored tokens, with an EXPIRED access token blanked out.

        The SDK keeps a token's expiry only in memory, so after a restart it cannot
        tell that a stored access token has aged out: it treats it as valid, sends
        it, gets 401, and jumps straight to a full browser re-authorisation —
        never trying the refresh token it is holding. Blanking the access token
        while keeping the refresh token makes `is_token_valid()` false and
        `can_refresh_token()` true, which is exactly the refresh path.
        """
        data = self._read()
        raw = data.get("tokens")
        if not raw:
            return None
        token = OAuthToken.model_validate(raw)
        expires_at = data.get("expires_at")
        # `is not None`, not truthiness: an expires_at of 0 is a real (long past) expiry.
        if token.refresh_token and expires_at is not None and time.time() >= float(expires_at) - 60:
            return token.model_copy(update={"access_token": ""})
        return token

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(exclude_none=True)
        data["tokens_saved_at"] = int(time.time())
        if tokens.expires_in:
            data["expires_at"] = int(time.time()) + int(tokens.expires_in)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client")
        if raw:
            return OAuthClientInformationFull.model_validate(raw)
        client_id = env("HUBSPOT_MCP_CLIENT_ID")
        if not client_id:
            return None
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=env("HUBSPOT_MCP_CLIENT_SECRET") or None,
            redirect_uris=[redirect_uri()],
            token_endpoint_auth_method="client_secret_post" if env("HUBSPOT_MCP_CLIENT_SECRET") else "none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="Bench Outreach Agents",
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client"] = client_info.model_dump(exclude_none=True, mode="json")
        self._write(data)

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))


def redirect_uri() -> str:
    return env("HUBSPOT_MCP_REDIRECT_URI") or DEFAULT_REDIRECT_URI


def client_metadata() -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="Bench Outreach Agents",
        redirect_uris=[redirect_uri()],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post" if env("HUBSPOT_MCP_CLIENT_SECRET") else "none",
    )


def _reauth_message() -> str:
    """Says which of the two situations this is, because the fix is the same but the
    surprise is not: never authorised, or authorised and the grant is no longer usable."""
    had = FileTokenStorage().has_tokens()
    what = ("the stored HubSpot tokens were rejected and could not be refreshed"
            if had else "HubSpot MCP has no stored tokens")
    return f"{what} — run `python scripts/hubspot_auth.py` to authorise again"


async def _headless_redirect(_url: str) -> None:
    raise NotAuthorised(_reauth_message())


async def _headless_callback() -> AuthorizationCodeResult:
    raise NotAuthorised(_reauth_message())


# ------------------------------------------------------------------- client
class HubSpotMCP:
    def __init__(
        self,
        server_url: str | None = None,
        storage: TokenStorage | None = None,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]] | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.server_url = server_url or env("HUBSPOT_MCP_URL") or DEFAULT_SERVER_URL
        self.storage = storage or FileTokenStorage()
        self._redirect = redirect_handler or _headless_redirect
        self._callback = callback_handler or _headless_callback
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._metadata: OAuthMetadata | None = None

    # -- sync façade for FastAPI handlers (run in a threadpool) and scripts
    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return asyncio.run(self.acall(tool, arguments))

    def list_tools(self) -> list[str]:
        return asyncio.run(self.alist_tools())

    # -- async core
    async def acall(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._session() as session:
                    result = await session.call_tool(tool, arguments or {})
                break
            except (httpx2.TransportError, httpx2.TimeoutException) as exc:
                if attempt > self._max_retries:
                    raise HubSpotMCPError(f"{tool}: transport failed after {attempt} tries: {exc}") from exc
                await self._sleep(0.5 * (2 ** (attempt - 1)))
            except NotAuthorised:
                raise
            except MCPError as exc:
                raise HubSpotMCPError(f"{tool}: {exc}") from exc
            except BaseExceptionGroup as eg:          # anyio task groups wrap the real cause
                inner = _first(eg, (NotAuthorised, MCPError, httpx2.HTTPError))
                if isinstance(inner, NotAuthorised):
                    raise inner
                raise HubSpotMCPError(f"{tool}: {inner or eg}") from eg
        if getattr(result, "is_error", False):
            raise HubSpotMCPError(f"{tool}: {_text(result)}")
        return _payload(result)

    async def alist_tools(self) -> list[str]:
        async with self._session() as session:
            listed = await session.list_tools()
        return [t.name for t in listed.tools]

    async def _discover_metadata(self) -> OAuthMetadata | None:
        """Fetch HubSpot's OAuth endpoints before the first request.

        The SDK only discovers these while handling a 401, so on a headless run it
        reaches the refresh step with none and falls back to guessing
        `<server>/token` — which HubSpot answers 404, and a failed refresh means a
        browser consent that nobody is there to give. One cheap GET up front makes
        the refresh use the real token endpoint.
        """
        if self._metadata is not None:
            return self._metadata
        async with httpx2.AsyncClient(timeout=self._timeout) as http:
            for url in build_oauth_authorization_server_metadata_discovery_urls(
                    None, self.server_url):
                try:
                    response = await http.send(create_oauth_metadata_request(url))
                except httpx2.HTTPError:
                    continue
                keep_trying, metadata = await handle_auth_metadata_response(response)
                if metadata is not None:
                    self._metadata = metadata
                    return metadata
                if not keep_trying:
                    break
        return None

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        """An initialised MCP session. Nested `async with` on purpose: the SDK's
        transport owns anyio task groups that must open and close in one task."""
        provider = OAuthClientProvider(
            server_url=self.server_url, client_metadata=client_metadata(),
            storage=self.storage, redirect_handler=self._redirect, callback_handler=self._callback)
        metadata = await self._discover_metadata()
        if metadata is not None:
            provider.context.oauth_metadata = metadata
        async with self._http_client(provider) as http:
            async with streamable_http_client(self.server_url, http_client=http) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=self._timeout) as session:
                    await session.initialize()
                    yield session

    def _http_client(self, auth: OAuthClientProvider) -> httpx2.AsyncClient:
        """Separated so tests can swap in an in-process ASGI transport."""
        return httpx2.AsyncClient(auth=auth, timeout=self._timeout)


def _first(eg: BaseException, types: tuple) -> BaseException | None:
    """Depth-first search of an exception group for the first exception of `types`."""
    for exc in getattr(eg, "exceptions", []):
        if isinstance(exc, types):
            return exc
        found = _first(exc, types)
        if found:
            return found
    return None


def _text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []) if getattr(c, "text", None))


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    text = _text(result)
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"result": parsed}
