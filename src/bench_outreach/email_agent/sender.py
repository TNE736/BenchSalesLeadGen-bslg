"""Delivery. Two implementations, one interface: Mailgun, and dry-run to a file."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..common.settings import env

MAILGUN_API_BASE = "https://api.mailgun.net/v3"
UNSUBSCRIBE_TOKEN = "%unsubscribe_url%"     # Mailgun substitutes a real link per recipient


class SendError(RuntimeError):
    """The message was refused. The lead is written FAILED with this reason."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None
    detail: str = ""


class Sender(Protocol):
    def send(self, to: str, subject: str, body: str,
             variables: dict[str, str], html: str = "") -> SendResult: ...


class DryRunSender:
    """Writes what would have been sent to a readable file. Sends nothing. The default —
    turning it off is an explicit flag."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.drafts: list[dict[str, Any]] = []

    def send(self, to: str, subject: str, body: str,
             variables: dict[str, str], html: str = "") -> SendResult:
        self.drafts.append({"to": to, "subject": subject, "body": body, **variables})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 78}\n"
                     f"TO      {to}\n"
                     f"CONTACT {variables.get('bo_object_id', '')}"
                     f"   technology: {variables.get('bo_technology', '')}"
                     f"   reference: {variables.get('bo_reference', '')}\n"
                     f"SUBJECT {subject}\n{'-' * 78}\n{body}\n")
        return SendResult(ok=True, message_id=None, detail=f"dry-run -> {self.path.name}")


class MailgunSender:
    """One send, one attempt. No retry: a timeout means "no answer", not "not sent",
    and retrying that ambiguity is how one person receives the same email twice."""

    def __init__(self, api_key: str | None = None, domain: str | None = None,
                 sender: str | None = None, reply_to: str | None = None,
                 client: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self.api_key = api_key or env("MAILGUN_API_KEY")
        self.domain = domain or env("MAILGUN_DOMAIN")
        self.sender = sender or env("MAILGUN_FROM")
        self.reply_to = reply_to if reply_to is not None else env("MAILGUN_REPLY_TO")
        self._client = client or httpx.Client(timeout=timeout)
        if not (self.api_key and self.domain and self.sender):
            raise SendError("MAILGUN_API_KEY, MAILGUN_DOMAIN and MAILGUN_FROM must all be set")

    def send(self, to: str, subject: str, body: str,
             variables: dict[str, str], html: str = "") -> SendResult:
        # Both parts: a client that cannot render HTML still gets the text, unsubscribe
        # link and all; open tracking needs the HTML part to live in.
        data: dict[str, Any] = {
            "from": self.sender, "to": to, "subject": subject, "text": body,
            "o:tracking": "yes", "o:tracking-opens": "yes", "o:tracking-clicks": "no",
        }
        if html:
            data["html"] = html
        if self.reply_to:
            data["h:Reply-To"] = self.reply_to
        for key, value in variables.items():    # echoed back on every Mailgun webhook
            data[f"v:{key}"] = value
        try:
            resp = self._client.post(f"{MAILGUN_API_BASE}/{self.domain}/messages",
                                     auth=("api", self.api_key), data=data)
        except (httpx.HTTPError, OSError) as exc:
            raise SendError(f"Mailgun unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise SendError(f"Mailgun refused the message: HTTP {resp.status_code}: {resp.text[:300]}")
        return SendResult(ok=True, message_id=resp.json().get("id"),
                          detail=f"accepted {int(time.time())}")
