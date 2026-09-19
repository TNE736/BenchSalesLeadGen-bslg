import pytest

from bench_outreach.gateway.signature import SignatureError, compute_v3, verify_v3

SECRET = "s3cret"
URI = "https://gw.example.com/hubspot/events"
BODY = '[{"eventId":1}]'


def _now_ms() -> str:
    return "1700000000000"


def test_round_trip_verifies():
    ts = _now_ms()
    sig = compute_v3(SECRET, "POST", URI, BODY, ts)
    verify_v3(SECRET, "POST", URI, BODY, ts, sig, now=1700000000.0)


@pytest.mark.parametrize("tamper", ["body", "uri", "secret", "signature"])
def test_any_change_breaks_verification(tamper):
    ts = _now_ms()
    sig = compute_v3(SECRET, "POST", URI, BODY, ts)
    body, uri, secret = BODY, URI, SECRET
    if tamper == "body":
        body = BODY + " "
    elif tamper == "uri":
        uri = URI.replace("https", "http")
    elif tamper == "secret":
        secret = "other"
    else:
        sig = "AAAA" + sig[4:]
    with pytest.raises(SignatureError):
        verify_v3(secret, "POST", uri, body, ts, sig, now=1700000000.0)


def test_stale_request_is_rejected():
    ts = _now_ms()
    sig = compute_v3(SECRET, "POST", URI, BODY, ts)
    with pytest.raises(SignatureError, match="older"):
        verify_v3(SECRET, "POST", URI, BODY, ts, sig, now=1700000000.0 + 301)


def test_future_request_is_rejected():
    ts = _now_ms()
    sig = compute_v3(SECRET, "POST", URI, BODY, ts)
    with pytest.raises(SignatureError, match="future"):
        verify_v3(SECRET, "POST", URI, BODY, ts, sig, now=1700000000.0 - 61)


@pytest.mark.parametrize("missing", ["secret", "signature", "timestamp"])
def test_missing_pieces_are_rejected(missing):
    ts = _now_ms()
    sig = compute_v3(SECRET, "POST", URI, BODY, ts)
    kwargs = dict(secret=SECRET, method="POST", uri=URI, body=BODY, timestamp=ts,
                  signature=sig, now=1700000000.0)
    kwargs[missing] = ""
    with pytest.raises(SignatureError):
        verify_v3(**kwargs)


def test_non_ascii_signature_is_401_not_crash():
    ts = _now_ms()
    with pytest.raises(SignatureError):
        verify_v3(SECRET, "POST", URI, BODY, ts, "é" * 10, now=1700000000.0)
