"""Unit tests for the Gemini-backed AI editor (ai_editor.py).

These exercise the pure request-building / response-parsing helpers and the
``propose`` network path with a stubbed httpx client — no real Gemini calls, so
they run offline and deterministically.
"""
from __future__ import annotations

import json

import pytest

import ai_editor

# A manifest is only used for section-type enums + tool names here; the empty
# dict exercises ai_editor's built-in fallbacks.
MANIFEST: dict = {}

SNAPSHOT: dict = {
    "store": {"_id": "s1", "name": "Acme", "tagline": "old"},
    "sections": [{"key": "hero", "type": "hero", "order": 1, "visible": True}],
    "items": [],
    "specials": [],
}


# ── response_schema ────────────────────────────────────────────────────────


def test_response_schema_constrains_tool_and_args():
    schema = ai_editor.response_schema(MANIFEST)
    assert schema["type"] == "OBJECT"
    assert schema["required"] == ["reply", "ops"]
    item = schema["properties"]["ops"]["items"]
    # tool is an enum of exactly the known tools; args is a JSON-string field.
    expected = [t["function"]["name"] for t in ai_editor.build_tools(MANIFEST)]
    assert item["properties"]["tool"]["enum"] == expected
    assert item["properties"]["args"]["type"] == "STRING"
    assert item["required"] == ["tool", "args"]


# ── build_payload ────────────────────────────────────────────────────────


def test_build_payload_is_json_mode():
    messages = [
        {"role": "user", "content": "change the tagline"},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "to 'fresh daily'"},
    ]
    payload = ai_editor.build_payload(messages, SNAPSHOT, MANIFEST)
    gen = payload["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseSchema"] == ai_editor.response_schema(MANIFEST)
    assert payload["systemInstruction"]["parts"][0]["text"]
    # assistant maps to the "model" role; blank turns are dropped.
    roles = [c["role"] for c in payload["contents"]]
    assert roles == ["user", "model", "user"]


# ── parse_gemini_response ────────────────────────────────────────────────


def _candidate(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parse_response_decodes_ops_with_json_string_args():
    body = json.dumps(
        {
            "reply": "",
            "ops": [
                {"tool": "update_store_info", "args": json.dumps({"tagline": "Fresh daily"})},
                {"tool": "add_section", "args": json.dumps({"type": "gallery", "title": "Work"})},
            ],
        }
    )
    out = ai_editor.parse_gemini_response(_candidate(body))
    assert "ops" in out
    assert out["ops"][0] == {"tool": "update_store_info", "args": {"tagline": "Fresh daily"}}
    assert out["ops"][1]["args"]["type"] == "gallery"


def test_parse_response_returns_reply_when_no_ops():
    body = json.dumps({"reply": "Which item did you mean?", "ops": []})
    out = ai_editor.parse_gemini_response(_candidate(body))
    assert out == {"reply": "Which item did you mean?"}


def test_parse_response_handles_block_and_empty():
    assert "reply" in ai_editor.parse_gemini_response({"promptFeedback": {"blockReason": "SAFETY"}})
    assert "reply" in ai_editor.parse_gemini_response({"candidates": []})


# ── propose (stubbed network) ────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Minimal async context-manager stand-in for httpx.AsyncClient."""

    last: dict = {}

    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"url": url, "headers": headers, "json": json}
        return self._resp


async def test_propose_requires_api_key(monkeypatch):
    monkeypatch.setattr(ai_editor, "GEMINI_API_KEY", "")
    with pytest.raises(ai_editor.AIEditorError):
        await ai_editor.propose([{"role": "user", "content": "hi"}], SNAPSHOT, MANIFEST)


async def test_propose_returns_validated_ops(monkeypatch):
    monkeypatch.setattr(ai_editor, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(ai_editor, "GEMINI_MODEL", "gemini-3.5-flash")
    body = json.dumps({"reply": "", "ops": [{"tool": "update_store_info", "args": "{\"tagline\": \"Hi\"}"}]})
    resp = _FakeResp(200, _candidate(body))
    monkeypatch.setattr(ai_editor.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))

    out = await ai_editor.propose([{"role": "user", "content": "set tagline to Hi"}], SNAPSHOT, MANIFEST)
    assert out == {"ops": [{"tool": "update_store_info", "args": {"tagline": "Hi"}}]}
    # It hit the right model endpoint with the API key header.
    assert "gemini-3.5-flash:generateContent" in _FakeClient.last["url"]
    assert _FakeClient.last["headers"]["x-goog-api-key"] == "test-key"


async def test_propose_maps_404_to_editor_error(monkeypatch):
    monkeypatch.setattr(ai_editor, "GEMINI_API_KEY", "test-key")
    resp = _FakeResp(404, {"error": {"message": "model not found"}})
    monkeypatch.setattr(ai_editor.httpx, "AsyncClient", lambda *a, **k: _FakeClient(resp))
    with pytest.raises(ai_editor.AIEditorError):
        await ai_editor.propose([{"role": "user", "content": "hi"}], SNAPSHOT, MANIFEST)
