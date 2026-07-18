"""AI editor resilience: retry on transient failures + fallback model on 404.

These stub the single network hop (``ai_editor._post_generate``) so they run
offline and deterministically, asserting the retry/backoff and model-fallback
policy in ``ai_editor.propose`` without touching Gemini.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_editor

_MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "manifest.json").read_text())
_SNAPSHOT = {"store": {}, "sections": [], "items": [], "specials": []}
_MESSAGES = [{"role": "user", "content": "change the tagline to Fresh"}]


class _FakeResp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def _ok_payload() -> dict:
    inner = json.dumps({"reply": "", "ops": [{"tool": "update_store_info", "args": "{\"tagline\": \"Fresh\"}"}]})
    return {"candidates": [{"content": {"parts": [{"text": inner}]}}]}


@pytest.fixture(autouse=True)
def _fast_ai(monkeypatch):
    """Enable the editor + zero backoff so retry tests don't actually sleep."""
    monkeypatch.setattr(ai_editor, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_editor, "GEMINI_MODEL", "gemini-primary")
    monkeypatch.setattr(ai_editor, "GEMINI_FALLBACK_MODEL", "gemini-fallback")
    monkeypatch.setattr(ai_editor, "GEMINI_MAX_RETRIES", 2)
    monkeypatch.setattr(ai_editor, "GEMINI_RETRY_BACKOFF", 0.0)


def _install_sequence(monkeypatch, responses):
    """Stub _post_generate to return queued responses and record the models tried."""
    calls: list[str] = []

    async def fake_post(client, model, payload):
        calls.append(model)
        return responses[len(calls) - 1]

    monkeypatch.setattr(ai_editor, "_post_generate", fake_post)
    return calls


async def test_retries_then_succeeds_on_429(monkeypatch):
    calls = _install_sequence(monkeypatch, [_FakeResp(429), _FakeResp(200, _ok_payload())])
    result = await ai_editor.propose(_MESSAGES, _SNAPSHOT, _MANIFEST)
    assert result["ops"][0]["tool"] == "update_store_info"
    assert len(calls) == 2  # one retry


async def test_falls_back_to_secondary_model_on_404(monkeypatch):
    calls = _install_sequence(monkeypatch, [_FakeResp(404), _FakeResp(200, _ok_payload())])
    result = await ai_editor.propose(_MESSAGES, _SNAPSHOT, _MANIFEST)
    assert "ops" in result
    assert calls == ["gemini-primary", "gemini-fallback"]


async def test_bad_key_is_not_retried(monkeypatch):
    calls = _install_sequence(monkeypatch, [_FakeResp(401)])
    with pytest.raises(ai_editor.AIEditorError):
        await ai_editor.propose(_MESSAGES, _SNAPSHOT, _MANIFEST)
    assert len(calls) == 1  # auth errors fail fast


async def test_exhausted_retries_raise_rate_limit(monkeypatch):
    calls = _install_sequence(monkeypatch, [_FakeResp(429), _FakeResp(429), _FakeResp(429)])
    with pytest.raises(ai_editor.AIEditorError):
        await ai_editor.propose(_MESSAGES, _SNAPSHOT, _MANIFEST)
    assert len(calls) == 3  # initial + GEMINI_MAX_RETRIES


async def test_missing_key_raises_before_network(monkeypatch):
    monkeypatch.setattr(ai_editor, "GEMINI_API_KEY", "")

    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("network should not be touched without a key")

    monkeypatch.setattr(ai_editor, "_post_generate", boom)
    with pytest.raises(ai_editor.AIEditorError):
        await ai_editor.propose(_MESSAGES, _SNAPSHOT, _MANIFEST)
