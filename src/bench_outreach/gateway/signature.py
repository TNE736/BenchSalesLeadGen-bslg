"""HubSpot request-signature v3: base64(HMAC-SHA256(secret, METHOD+URI+BODY+TS))."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

MAX_FUTURE_SKEW_SECONDS = 60


class SignatureError(Exception):
    """Missing, stale or wrong signature. Always answered with 401."""


def compute_v3(secret: str, method: str, uri: str, body: str, timestamp: str) -> str:
    message = f"{method.upper()}{uri}{body}{timestamp}"
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_v3(
    secret: str,
    method: str,
    uri: str,
    body: str,
    timestamp: str,
    signature: str,
    max_age_seconds: int = 300,
    now: float | None = None,
) -> None:
    """Raise SignatureError unless the delivery is authentic and fresh.

    ``uri`` must be the exact URL HubSpot signed (scheme + host + path). Behind a
    tunnel or proxy that rewrites Host, pass the public URL, not request.url.
    """
    if not secret:
        raise SignatureError("HUBSPOT_APP_CLIENT_SECRET is not set")
    if not signature:
        raise SignatureError("missing X-HubSpot-Signature-v3")
    if not timestamp:
        raise SignatureError("missing X-HubSpot-Request-Timestamp")
    try:
        sent_at = int(timestamp) / 1000.0
    except ValueError:
        raise SignatureError("timestamp is not epoch milliseconds") from None

    age = (time.time() if now is None else now) - sent_at
    if age > max_age_seconds:
        raise SignatureError("request is older than the accepted window")
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise SignatureError("request timestamp is in the future")

    expected = compute_v3(secret, method, uri, body, timestamp)
    if not hmac.compare_digest(expected.encode(), signature.encode("utf-8", "replace")):
        raise SignatureError("signature mismatch")
