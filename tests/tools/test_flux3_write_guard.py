"""Regression: bfl_flux3_get_result save_to must be write-guarded.

The flux3 poll tool downloads the finished clip in-process on the host, so
its model-controlled ``save_to`` bypasses the terminal backend entirely. It
must refuse the same destinations the TTS ``output_path`` refuses — ``..``
traversal, protected credential/system paths, and (when
``HERMES_WRITE_SAFE_ROOT`` is set) anything outside the safe roots — before
any gateway work, while an ordinary destination keeps working.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tools import flux3_video_tool as flux3

GATEWAY = "https://tool-gateway.example.com"
BASE_URL = f"{GATEWAY}/api/bfl"
UPLOAD_PATH = "/api/uploads/bfl"

# Comfortably above _MIN_PLAUSIBLE_VIDEO_BYTES so the saved clip is accepted.
_CLIP = b"x" * (128 * 1024)


@pytest.fixture(autouse=True)
def _endpoints():
    """Every test runs as if the mount is reachable."""
    with patch.object(
        flux3,
        "managed_vendor_endpoints",
        return_value={"origin": GATEWAY, "base_url": BASE_URL, "upload_path": UPLOAD_PATH},
    ):
        yield


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    def __init__(self, response, sink):
        self._response = response
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def request(self, method, url, headers=None, json=None):
        self._sink.append({"method": method, "url": url})
        return self._response


class _FakeStream:
    def __init__(self, body):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self._body


@contextmanager
def _fake_download(body):
    """Stub the clip download behind the SSRF-guarded client."""
    from tools import url_safety

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, _method, _url):
            return _FakeStream(body)

    with patch.object(url_safety, "create_ssrf_safe_async_client", lambda **_kw: _Client()):
        yield


def _poll(args, response):
    """Invoke _handle_get_result with the transport stubbed; (parsed, requests)."""
    import httpx

    sink = []
    with patch.object(
        flux3, "managed_gateway_auth_headers", return_value={"Authorization": "Bearer nous-token"}
    ), patch.object(httpx, "AsyncClient", lambda **_kw: _FakeClient(response, sink)):
        raw = asyncio.run(flux3._handle_get_result(args))
    return json.loads(raw), sink


def _terminal_response():
    """A finished-job poll body: served only if the guard wrongly lets one through."""
    return _FakeResponse(200, {"id": "bfl_job_1", "status": "Error", "guidance": "The job is over."})


def _ready_response():
    return _FakeResponse(200, {
        "id": "bfl_job_1",
        "status": "Ready",
        "result": {"sample": "https://cdn.example/x/flux3-clip.mp4?sig=a"},
        "guidance": "Deliver the saved file.",
    })


def _point_file_safety_at(monkeypatch, hermes_home):
    import agent.file_safety as file_safety

    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: hermes_home)
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: hermes_home)


def test_save_to_rejects_traversal_escape():
    """A save_to with '..' components must be refused before any gateway work."""
    parsed, requests = _poll(
        {"id": "bfl_job_1", "save_to": "videos/../../etc/cron.d/malicious"},
        _terminal_response(),
    )

    assert "error" in parsed
    assert "traversal" in parsed["error"].lower()
    assert requests == []


def test_save_to_rejects_hermes_oauth_store(tmp_path, monkeypatch):
    """save_to must not bypass the shared protected-file write guard."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    _point_file_safety_at(monkeypatch, hermes_home)

    target = hermes_home / ".anthropic_oauth.json"
    parsed, requests = _poll({"id": "bfl_job_1", "save_to": str(target)}, _terminal_response())

    assert "error" in parsed
    assert "save_to" in parsed["error"]
    assert requests == []
    assert not target.exists()


def test_save_to_rejects_mcp_token_directory(tmp_path, monkeypatch):
    """A directory-form save_to must not land vendor bytes among MCP tokens."""
    hermes_home = tmp_path / "hermes-home"
    token_dir = hermes_home / "mcp-tokens"
    token_dir.mkdir(parents=True)
    _point_file_safety_at(monkeypatch, hermes_home)

    parsed, requests = _poll({"id": "bfl_job_1", "save_to": str(token_dir)}, _terminal_response())

    assert "error" in parsed
    assert "save_to" in parsed["error"]
    assert requests == []
    assert list(token_dir.iterdir()) == []


def test_save_to_outside_write_safe_root_is_refused(tmp_path, monkeypatch):
    """A HERMES_WRITE_SAFE_ROOT install must contain this in-process write too."""
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(safe_root))

    outside = tmp_path / "outside" / "clip.mp4"
    parsed, requests = _poll({"id": "bfl_job_1", "save_to": str(outside)}, _terminal_response())

    assert "error" in parsed
    assert requests == []
    assert not outside.parent.exists()

    # And the guard is no wider than the policy: inside the root still works.
    inside = safe_root / "clip.mp4"
    with _fake_download(_CLIP):
        parsed, _requests = _poll({"id": "bfl_job_1", "save_to": str(inside)}, _ready_response())

    assert "error" not in parsed
    assert parsed["details"]["saved_path"] == str(inside)
    assert inside.read_bytes() == _CLIP


def test_an_ordinary_save_to_still_succeeds(tmp_path):
    """The guard must not break the normal save path."""
    with _fake_download(_CLIP):
        parsed, requests = _poll({"id": "bfl_job_1", "save_to": str(tmp_path)}, _ready_response())

    saved = tmp_path / "flux3-clip.mp4"
    assert "error" not in parsed
    assert parsed["details"]["saved_path"] == str(saved)
    assert saved.read_bytes() == _CLIP
    assert len(requests) == 1
