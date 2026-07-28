"""Regression tests for runtime BDH endpoint configuration."""

import importlib.util
from pathlib import Path


def test_bdh_request_resolves_endpoint_after_module_import(monkeypatch):
    """A runtime BDH_API_URL change must override the import-time endpoint."""
    module_path = Path(__file__).with_name("__init__.py")
    monkeypatch.setenv("BDH_API_URL", "http://import.example:8643")
    spec = importlib.util.spec_from_file_location("bdh_bridge_issue10", module_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load bridge module")
    runtime_bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime_bridge)

    monkeypatch.setenv("BDH_API_URL", "http://runtime.example:8643")
    captured_urls = []

    class FakeResp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured_urls.append(req.full_url)
        return FakeResp()

    monkeypatch.setattr(runtime_bridge.urllib.request, "urlopen", fake_urlopen)
    assert runtime_bridge._bdh_request("/api/query", {"query": "test"}) == {}
    assert captured_urls == ["http://runtime.example:8643/api/query"]
