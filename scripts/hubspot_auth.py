"""One-time HubSpot MCP consent. Run it once per machine; agents are headless after.

    python scripts/hubspot_auth.py            # opens the browser, stores tokens, proves access
    python scripts/hubspot_auth.py --check    # no browser: verify the stored tokens still work

Needs in .env:  HUBSPOT_MCP_CLIENT_ID, HUBSPOT_MCP_CLIENT_SECRET  (from the MCP connector
you created in HubSpot → Development → MCP Connectors, whose Redirect URL must be exactly
HUBSPOT_MCP_REDIRECT_URI, default http://localhost:8765/callback).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bench_outreach.common.hubspot_mcp import (FileTokenStorage, HubSpotMCP,  # noqa: E402
                                               HubSpotMCPError, redirect_uri)
from bench_outreach.common.settings import env  # noqa: E402
from mcp.shared.auth import AuthorizationCodeResult  # noqa: E402


class _Callback:
    """Tiny local HTTP server that catches HubSpot's redirect and hands back code+state."""

    def __init__(self, uri: str) -> None:
        parsed = urlparse(uri)
        self.path = parsed.path or "/callback"
        self.result: asyncio.Future | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                ok = urlparse(self.path).path == outer.path and "code" in q
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(b"<h3>Bench Outreach: HubSpot connected. You can close this tab.</h3>"
                                 if ok else b"<h3>Missing code in callback.</h3>")
                if ok and outer.loop and outer.result and not outer.result.done():
                    outer.loop.call_soon_threadsafe(
                        outer.result.set_result,
                        AuthorizationCodeResult(code=q["code"][0], state=(q.get("state") or [None])[0],
                                                iss=(q.get("iss") or [None])[0]))

            def log_message(self, *_: object) -> None:  # silence
                pass

        host = parsed.hostname or "localhost"
        bind = "127.0.0.1" if host in ("localhost", "127.0.0.1") else host
        self.server = HTTPServer((bind, parsed.port or 80), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"Listening for HubSpot's redirect on http://{bind}:{parsed.port or 80}{self.path}")

    @staticmethod
    def parse_pasted(url: str) -> AuthorizationCodeResult | None:
        q = parse_qs(urlparse(url.strip()).query)
        if "code" not in q:
            return None
        return AuthorizationCodeResult(code=q["code"][0], state=(q.get("state") or [None])[0],
                                       iss=(q.get("iss") or [None])[0])

    async def redirect(self, url: str) -> None:
        print("\nOpening HubSpot consent in your browser. If it does not open, visit:\n  " + url + "\n")
        webbrowser.open(url)

    async def wait(self) -> AuthorizationCodeResult:
        """Whichever comes first: the browser hits our callback, or the user pastes
        the redirected URL (for when the browser shows 'localhost refused')."""
        self.loop = asyncio.get_running_loop()
        self.result = self.loop.create_future()
        print("Waiting for HubSpot to redirect back ...")
        print("If the browser ends on 'This site can't be reached', copy the FULL URL from its\n"
              "address bar (it starts with http://localhost:8765/callback?code=...) and paste it here:")

        def read_stdin() -> None:
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001
                return
            parsed = self.parse_pasted(line) if line else None
            if parsed and self.loop and self.result and not self.result.done():
                self.loop.call_soon_threadsafe(self.result.set_result, parsed)

        threading.Thread(target=read_stdin, daemon=True).start()
        return await asyncio.wait_for(self.result, timeout=600)


async def main(check_only: bool) -> int:
    storage = FileTokenStorage()
    if not check_only and not env("HUBSPOT_MCP_CLIENT_ID"):
        print("HUBSPOT_MCP_CLIENT_ID is not set in .env — create the MCP connector in HubSpot first.")
        return 2
    if check_only and not storage.has_tokens():
        print(f"No tokens at {storage.path}. Run without --check to authorise.")
        return 2

    cb = None if check_only else _Callback(redirect_uri())
    # The browser sign-in happens INSIDE the first MCP request, so that request must
    # be allowed to wait for a human: 10 minutes here, 30 s for headless agents.
    hs = HubSpotMCP(storage=storage,
                    redirect_handler=cb.redirect if cb else None,
                    callback_handler=cb.wait if cb else None,
                    timeout_seconds=600 if cb else 30)
    try:
        who = await hs.acall("get_user_details", {"include": ["USER_INFORMATION"]})
        tools = await hs.alist_tools()
    except HubSpotMCPError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        if cb:
            cb.server.shutdown()

    info = who.get("userInformation", {})
    print(f"\nConnected. account={who.get('accountId')}  user={info.get('email')}")
    print(f"tokens stored at {storage.path}")
    print(f"{len(tools)} tools; CRM write available: {'manage_crm_objects' in tools}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    raise SystemExit(asyncio.run(main(ap.parse_args().check)))
