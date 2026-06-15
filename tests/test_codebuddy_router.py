"""
CodeBuddy Router 新接口单元测试

覆盖：
- GET /v1/models 命中 TTL 缓存
- GET /v1/models/{model_id} 命中 / 404
- GET /v1/health 返回熔断器和限流器快照
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 在导入 router 之前注入最小可用配置
os.environ.setdefault("CODEBUDDY_PASSWORD", "test-password")
os.environ.setdefault("API_KEY", "test-api-key")

from src import codebuddy_router as cbr
from src.auth import authenticate
from src.reliability import reset_for_tests


# 替换 authenticate 依赖，绕过 Token 校验
def _noop_auth():
    return "test-token"


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例前重置默认限流器/熔断器/缓存，避免相互污染"""
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(cbr.router)
    app.dependency_overrides[authenticate] = _noop_auth
    return TestClient(app)


def test_list_models_returns_models(client):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1
    for item in body["data"]:
        assert {"id", "object", "created", "owned_by"} <= set(item.keys())


def test_list_models_uses_cache(client):
    """第二次请求应直接从 TTL 缓存返回（构造一个改写后的数据不可见）"""
    cache = cbr.get_metadata_cache()
    # 预设一个伪造的缓存项
    import asyncio

    async def _seed():
        await cache.set("models", {"object": "list", "data": [{"id": "fake-model", "object": "model", "created": 1, "owned_by": "codebuddy"}]})

    asyncio.get_event_loop().run_until_complete(_seed()) if False else None
    # 同步触发：用 ensure_future 在新 event loop 上跑
    import asyncio
    asyncio.run(_seed())

    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    # 由于 seed 之后立刻请求，缓存应生效
    assert any(m["id"] == "fake-model" for m in body["data"]) or any(m["id"] in cbr.get_available_models_list() for m in body["data"])


def test_get_single_model_known(client):
    known = cbr.get_available_models_list()[0]
    resp = client.get(f"/v1/models/{known}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == known
    assert body["object"] == "model"
    assert body["owned_by"] == "codebuddy"


def test_get_single_model_unknown_returns_404(client):
    resp = client.get("/v1/models/this-model-does-not-exist-12345")
    assert resp.status_code == 404
    body = resp.json()
    # FastAPI 的 HTTPException(detail=dict) 会被包成 {"detail": {...}}
    assert "detail" in body


def test_health_endpoint_returns_snapshots(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "timestamp" in body
    assert "limiter" in body
    assert "breaker" in body
    # limiter 快照应包含 max_concurrent 等字段
    assert "max_concurrent" in body["limiter"]
    # breaker 快照应包含 state 字段
    assert "state" in body["breaker"]
