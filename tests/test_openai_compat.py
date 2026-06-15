"""
OpenAI 兼容接口补齐的单元测试

覆盖：
- RequestProcessor._normalize_response_format
- RequestProcessor._apply_json_format_hint
- RequestProcessor.prepare_payload 的参数透传与白名单
- _reshape_completions_response / _reshape_completions_sse 的重塑
- /v1/embeddings 返回 OpenAI 风格 501 错误
- /v1/completions 输入校验与 404
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("CODEBUDDY_PASSWORD", "test-password")
os.environ.setdefault("API_KEY", "test-api-key")

from src import codebuddy_router as cbr
from src.auth import authenticate
from src.reliability import reset_for_tests


def _noop_auth():
    return "test-token"


@pytest.fixture(autouse=True)
def _reset_state():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(cbr.router)
    app.dependency_overrides[authenticate] = _noop_auth
    return TestClient(app)


# ============ prepare_payload ============

def test_normalize_response_format_dict_passthrough():
    out = cbr.RequestProcessor._normalize_response_format({"type": "json_object"})
    assert out == {"type": "json_object"}


def test_normalize_response_format_string_expands():
    assert cbr.RequestProcessor._normalize_response_format("json_object") == {"type": "json_object"}
    assert cbr.RequestProcessor._normalize_response_format("json_schema") == {"type": "json_schema"}


def test_normalize_response_format_unknown_returns_none():
    assert cbr.RequestProcessor._normalize_response_format("text") is None
    assert cbr.RequestProcessor._normalize_response_format(42) is None
    assert cbr.RequestProcessor._normalize_response_format(None) is None


def test_apply_json_format_hint_inserts_when_no_system():
    messages = [{"role": "user", "content": "hi"}]
    out = cbr.RequestProcessor._apply_json_format_hint(messages)
    assert out[0]["role"] == "system"
    assert "json" in out[0]["content"].lower()
    assert out[1] == messages[0]


def test_apply_json_format_hint_appends_to_existing_system():
    messages = [
        {"role": "system", "content": "You are X."},
        {"role": "user", "content": "hi"},
    ]
    out = cbr.RequestProcessor._apply_json_format_hint(messages)
    assert out is messages
    assert "You are X." in out[0]["content"]
    assert "json" in out[0]["content"].lower()


def test_prepare_payload_passes_through_openai_params():
    body = {
        "model": "codebuddy",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 256,
        "max_completion_tokens": 512,
        "frequency_penalty": 0.1,
        "presence_penalty": 0.2,
        "stop": ["END"],
        "seed": 42,
        "user": "u-1",
    }
    payload = cbr.RequestProcessor.prepare_payload(body)
    assert payload["temperature"] == 0.7
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 256
    assert payload["max_completion_tokens"] == 512
    assert payload["frequency_penalty"] == 0.1
    assert payload["presence_penalty"] == 0.2
    assert payload["stop"] == ["END"]
    assert payload["seed"] == 42
    assert payload["user"] == "u-1"
    assert payload["stream"] is True


def test_prepare_payload_strips_unknown_fields():
    body = {
        "model": "codebuddy",
        "messages": [{"role": "user", "content": "hi"}],
        "unknown_field": "should_be_dropped",
        "another_unknown": 123,
    }
    payload = cbr.RequestProcessor.prepare_payload(body)
    assert "unknown_field" not in payload
    assert "another_unknown" not in payload


def test_prepare_payload_response_format_json_object_injects_system_hint():
    body = {
        "model": "codebuddy",
        "messages": [{"role": "user", "content": "return json"}],
        "response_format": "json_object",
    }
    payload = cbr.RequestProcessor.prepare_payload(body)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["role"] == "system"
    assert "json" in payload["messages"][0]["content"].lower()


def test_prepare_payload_response_format_dict_passthrough():
    body = {
        "model": "codebuddy",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }
    payload = cbr.RequestProcessor.prepare_payload(body)
    assert payload["response_format"] == {"type": "json_object"}


def test_prepare_payload_response_format_unknown_value_dropped():
    body = {
        "model": "codebuddy",
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": "weird_format",
    }
    payload = cbr.RequestProcessor.prepare_payload(body)
    assert "response_format" not in payload


# ============ reshape helpers ============

def test_reshape_completions_response_basic():
    chat = {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "codebuddy",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"},
            {"index": 1, "message": {"role": "assistant", "content": "world"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    out = cbr._reshape_completions_response(chat, "codebuddy")
    assert out["object"] == "text_completion"
    assert out["choices"][0]["text"] == "hello"
    assert out["choices"][1]["text"] == "world"
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"] == chat["usage"]


def test_reshape_completions_response_missing_usage_omits():
    chat = {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "codebuddy",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
    out = cbr._reshape_completions_response(chat, "codebuddy")
    assert "usage" not in out
    assert out["choices"][0]["finish_reason"] == "stop"


def test_reshape_completions_sse_basic_chunk():
    line = 'data: {"id":"x","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    out = cbr._reshape_completions_sse(line, "codebuddy")
    assert "data: " in out
    body = out.split("data: ", 1)[1].split("\n", 1)[0]
    import json as _json
    obj = _json.loads(body)
    assert obj["object"] == "text_completion"
    assert obj["choices"][0]["text"] == "hi"


def test_reshape_completions_sse_passthrough_done():
    out = cbr._reshape_completions_sse("data: [DONE]\n\n", "m")
    assert out.startswith("data: [DONE]")


def test_reshape_completions_sse_non_data_line_unchanged():
    raw = ": keep-alive comment\n\n"
    assert cbr._reshape_completions_sse(raw, "m") == raw


# ============ /v1/embeddings endpoint ============

def test_embeddings_returns_501_with_openai_error(client):
    resp = client.post(
        "/v1/embeddings",
        json={"input": "hello world", "model": "codebuddy"},
    )
    assert resp.status_code == 501
    body = resp.json()
    assert "error" in body
    err = body["error"]
    assert err["type"] == "unsupported_request"
    assert err["code"] == "model_not_supported"
    assert "not supported" in err["message"].lower()


def test_embeddings_missing_input_400(client):
    resp = client.post("/v1/embeddings", json={"model": "codebuddy"})
    assert resp.status_code == 400


def test_embeddings_missing_model_400(client):
    resp = client.post("/v1/embeddings", json={"input": "hi"})
    assert resp.status_code == 400


def test_embeddings_invalid_json_400(client):
    resp = client.post(
        "/v1/embeddings",
        content=b"{not json}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ============ /v1/completions endpoint ============

def test_completions_missing_prompt_400(client):
    resp = client.post("/v1/completions", json={"model": "codebuddy"})
    assert resp.status_code == 400


def test_completions_empty_prompt_400(client):
    resp = client.post("/v1/completions", json={"model": "codebuddy", "prompt": "   "})
    assert resp.status_code == 400


def test_completions_wrong_prompt_type_400(client):
    resp = client.post("/v1/completions", json={"model": "codebuddy", "prompt": 123})
    assert resp.status_code == 400


def test_completions_unknown_model_404(client):
    resp = client.post(
        "/v1/completions",
        json={"model": "definitely-not-a-model-xyz", "prompt": "hi"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "detail" in body
    assert "not found" in str(body["detail"]).lower()


def test_completions_invalid_json_400(client):
    resp = client.post(
        "/v1/completions",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
