"""Retry safety for non-idempotent BDH writes.

urllib wraps failures that happen while connecting or sending in ``URLError``.
Anything raised after the request was fully sent (waiting for the response,
reading or decoding the body) or an HTTP error response means BDH may already
have run plasticity/neurogenesis. Retrying those would double-learn, so a
non-idempotent request (``retry_on_timeout=False``) must stop after one attempt.
"""
import errno
import http.client
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bdh_bridge_retry_safety", Path(__file__).with_name("__init__.py")
)
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _attempts(monkeypatch, outcome, **kwargs):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.get_method())
        if isinstance(outcome, BaseException):
            raise outcome
        return _Body(outcome)

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bridge, "_current_bdh_api", lambda: "http://127.0.0.1:18643")
    result = bridge._bdh_request("/api/query", retries=3, **kwargs)
    return result, calls


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("timed out"),  # response wait / body read timeout
        OSError(errno.ETIMEDOUT, "timed out"),
        ConnectionResetError(errno.ECONNRESET, "reset after send"),
        http.client.RemoteDisconnected("closed without response"),
        URLError(TimeoutError("timed out")),  # connect/send timeout: stays conservative
        HTTPError("http://127.0.0.1:18643/api/query", 500, "error", {}, io.BytesIO(b"")),
        b"not json",  # a response arrived, so the write may have been applied
    ],
    ids=["read-timeout", "errno-timeout", "reset", "remote-disconnected",
         "wrapped-timeout", "http-500", "bad-json"],
)
def test_non_idempotent_write_never_retries_an_ambiguous_outcome(monkeypatch, outcome):
    result, calls = _attempts(monkeypatch, outcome, data={"query": "q"}, retry_on_timeout=False)
    assert result is None
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "outcome, kwargs",
    [
        # Refused before anything was sent: the server never saw the write.
        (URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
         {"data": {"query": "q"}, "retry_on_timeout": False}),
        # Idempotent reads keep retrying timeouts as before.
        (TimeoutError("timed out"), {}),
    ],
    ids=["write-refused", "read-timeout-get"],
)
def test_failures_proven_harmless_are_still_retried(monkeypatch, outcome, kwargs):
    result, calls = _attempts(monkeypatch, outcome, **kwargs)
    assert result is None
    assert len(calls) == 3
