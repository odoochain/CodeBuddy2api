"""
IMA 客户端（ima_client.py）单元测试

覆盖：
- 成功路径（200 业务正常）
- 业务错误（code != 0）
- 网络层 5xx 重试
- 非 JSON 响应
- 凭证缺失
- 知识库 / 笔记 API 参数透传
"""
import json
import pytest
import httpx

from src.ima_client import IMAClient, IMAError


# -------------------- MockTransport 工具 --------------------
def make_mock_transport(handler):
    """把 handler 注入 httpx.MockTransport"""
    return httpx.MockTransport(handler)


def json_response(code: int = 0, data=None, msg: str = "ok", request_id: str = "rid-1"):
    """构造业务响应"""
    body = {"code": code, "msg": msg, "data": data or {}, "request_id": request_id}
    return httpx.Response(200, json=body)


# -------------------- 凭证解析 --------------------
def test_resolve_credentials_from_env(monkeypatch):
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "env_cid")
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "env_key")
    cid, key = IMAClient._resolve_credentials(None, None)
    assert cid == "env_cid"
    assert key == "env_key"


def test_resolve_credentials_explicit_overrides_env(monkeypatch):
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", "env_cid")
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", "env_key")
    cid, key = IMAClient._resolve_credentials("arg_cid", "arg_key")
    assert cid == "arg_cid"
    assert key == "arg_key"


def test_resolve_credentials_missing(monkeypatch, tmp_path):
    """当环境变量、配置文件、config 模块均无凭证时，应返回 (None, None)"""
    monkeypatch.delenv("IMA_OPENAPI_CLIENTID", raising=False)
    monkeypatch.delenv("IMA_OPENAPI_APIKEY", raising=False)
    # Windows 上 Path.home() 使用 USERPROFILE，需同时覆盖
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # 拦截 config 模块兜底
    import config
    monkeypatch.setattr(config, "get_ima_client_id", lambda: None, raising=False)
    monkeypatch.setattr(config, "get_ima_api_key", lambda: None, raising=False)

    cid, key = IMAClient._resolve_credentials(None, None)
    assert cid is None or cid == "", f"got cid={cid!r}"
    assert key is None or key == "", f"got key={key!r}"


# -------------------- 业务成功 --------------------
@pytest.mark.asyncio
async def test_search_knowledge_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        body = json.loads(request.content)
        captured["body"] = body
        return json_response(data={"results": [
            {"id": "1", "title": "Doc1", "content": "Hello world"}
        ]})

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        results = await client.search_knowledge(query="hello", top_k=3)
    finally:
        await client.aclose()

    assert len(results) == 1
    assert results[0]["title"] == "Doc1"
    assert captured["body"]["query"] == "hello"
    assert captured["body"]["top_k"] == 3
    # 凭证在 conftest autouse fixture 中通过环境变量注入
    assert captured["headers"].get("x-ima-clientid") == "test_client_id_abc"


@pytest.mark.asyncio
async def test_import_doc_returns_note_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(data={"note_id": "n-123"})

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        note_id = await client.import_doc(title="t", content="c")
    finally:
        await client.aclose()
    assert note_id == "n-123"


# -------------------- 业务错误 --------------------
@pytest.mark.asyncio
async def test_business_error_code_10001():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(code=10001, msg="参数非法", data=None)

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        with pytest.raises(IMAError) as exc_info:
            await client.list_knowledge_bases()
    finally:
        await client.aclose()
    assert exc_info.value.code == 10001
    assert "参数非法" in exc_info.value.msg


@pytest.mark.asyncio
async def test_business_error_code_10002():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(code=10002, msg="凭证无效")

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        with pytest.raises(IMAError) as exc_info:
            await client.search_docs(query="x")
    finally:
        await client.aclose()
    assert exc_info.value.code == 10002


# -------------------- 非 JSON 响应 --------------------
@pytest.mark.asyncio
async def test_non_json_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        with pytest.raises(IMAError) as exc_info:
            await client.search_knowledge(query="x")
    finally:
        await client.aclose()
    assert "非 JSON" in exc_info.value.msg


# -------------------- 5xx 重试 --------------------
@pytest.mark.asyncio
async def test_5xx_retries_then_raises():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502, text="bad gateway")

    client = IMAClient(max_retries=1)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        with pytest.raises(IMAError) as exc_info:
            await client.list_knowledge_bases()
    finally:
        await client.aclose()
    # max_retries=1 + 首次 = 2 次
    assert attempts["n"] == 2
    assert "502" in str(exc_info.value.msg) or "上游" in str(exc_info.value.msg)


@pytest.mark.asyncio
async def test_5xx_eventually_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            return httpx.Response(503, text="busy")
        return json_response(data={"knowledge_bases": []})

    client = IMAClient(max_retries=2)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        bases = await client.list_knowledge_bases()
    finally:
        await client.aclose()
    assert attempts["n"] == 2
    assert bases == []


# -------------------- 凭证缺失 --------------------
@pytest.mark.asyncio
async def test_missing_credentials_raises():
    """无任何凭证时调用应抛出 IMAError"""
    client = IMAClient()
    # 强制清空凭证，模拟缺失场景
    client.client_id = None
    client.api_key = None
    with pytest.raises(IMAError) as exc_info:
        await client.search_knowledge(query="x")
    assert "凭证未配置" in exc_info.value.msg


# -------------------- 笔记追加 --------------------
@pytest.mark.asyncio
async def test_append_doc_success():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["note_id"] == "n-1"
        return json_response(data={"success": True})

    client = IMAClient(max_retries=0)
    client._client = httpx.AsyncClient(
        base_url=client.base_url, transport=make_mock_transport(handler)
    )
    try:
        ok = await client.append_doc(note_id="n-1", content="more")
    finally:
        await client.aclose()
    assert ok is True


# -------------------- 上下文管理器 --------------------
@pytest.mark.asyncio
async def test_async_context_manager():
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(data={"results": []})

    async with IMAClient(max_retries=0) as client:
        client._client = httpx.AsyncClient(
            base_url=client.base_url, transport=make_mock_transport(handler)
        )
        results = await client.search_knowledge(query="x")
    assert results == []
