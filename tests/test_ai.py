"""AIClient contract — malformed output must fall through the chain and never
escape as a raw JSONDecodeError (review E2: raw escapes bypass every handler's
`except AIError` and leave the user in silence)."""
import asyncio
import json

import httpx
import pytest

import app.ai as ai
from app.ai import AIClient, AIError

VALID = json.dumps({"operations": [], "clarify": None})
BAD_JSON = '{"operations": [{"op": "update_medicine"}]  5'   # braces, invalid inside
TRUNCATED = '{"operations": [{"op": "update_medi'            # truncated (max_tokens) → both braces missing
NO_JSON = 'no model output without braces'


class _Resp:
    def __init__(self, content=None, status_code=200, payload=None):
        self.status_code = status_code
        self._content = content
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return {"choices": [{"message": {"content": self._content}}]}


def _client_factory(attempted: list, responses: list):
    class _FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def post(self, url, json=None, headers=None):
            attempted.append(json["model"])
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    return _FakeClient


def _run(client: AIClient) -> None:
    asyncio.run(client.complete_json_chain("hi", "hi"))


def test_garbled_json_falls_through_to_next_model(monkeypatch):
    attempted = []
    responses = [_Resp(BAD_JSON), _Resp(VALID)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    out, model = asyncio.run(AIClient(api_key="x", models=["m1", "m2"]).complete_json_chain("hi", "hi"))
    assert attempted == ["m1", "m2"]           # the chain moved on instead of dying
    assert model == "m2"
    assert out == {"operations": [], "clarify": None}


def test_truncated_and_braceless_output_fall_through(monkeypatch):
    attempted = []
    responses = [_Resp(TRUNCATED), _Resp(NO_JSON), _Resp(VALID)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    out, model = asyncio.run(AIClient(api_key="x", models=["m1", "m2", "m3"]).complete_json_chain("hi", "hi"))
    assert attempted == ["m1", "m2", "m3"]
    assert model == "m3"


def test_unexpected_payload_shape_falls_through(monkeypatch):
    attempted = []
    responses = [_Resp(None, payload={"error": {"message": "blocked"}}), _Resp(VALID)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    out, model = asyncio.run(AIClient(api_key="x", models=["m1", "m2"]).complete_json_chain("hi", "hi"))
    assert model == "m2"


def test_network_error_falls_through(monkeypatch):
    attempted = []
    responses = [httpx.ConnectTimeout("boom"), _Resp(VALID)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    out, model = asyncio.run(AIClient(api_key="x", models=["m1", "m2"]).complete_json_chain("hi", "hi"))
    assert model == "m2"


def test_all_models_garbage_raises_aierror(monkeypatch):
    attempted = []
    responses = [_Resp(BAD_JSON), _Resp(NO_JSON)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    with pytest.raises(AIError):
        _run(AIClient(api_key="x", models=["m1", "m2"]))
    assert attempted == ["m1", "m2"]


def test_hard_4xx_still_aborts_immediately(monkeypatch):
    attempted = []
    responses = [_Resp(None, status_code=400), _Resp(VALID)]
    monkeypatch.setattr(ai.httpx, "AsyncClient", _client_factory(attempted, responses))
    with pytest.raises(AIError):
        _run(AIClient(api_key="x", models=["m1", "m2"]))
    assert attempted == ["m1"]  # bad-key failures must not burn the chain
