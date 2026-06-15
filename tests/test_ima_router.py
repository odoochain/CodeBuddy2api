"""
IMA 路由层（ima_router.py）单元测试

覆盖：
- 模型判定（is_ima_model）
- 独立 REST 端点（知识库/笔记）
- OpenAI 兼容代理（ima-knowledge-qa / ima-notes-search / ima-notes-create）
- 错误处理（业务错误 -> HTTP 状态码、凭证缺失 -> 503）
- 消息提取与上下文格式化
"""
import json
import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ima_client import IMAClient, IMAError
from src.ima_router import (
    IMA_MODEL_IDS,
    is_ima_model,
    ima_list_models,
    _extract_last_user_message,
    _format_results_as_context,
    _format_results_for_notes,
    router as ima_router,
)


# -------------------- 模型判定 --------------------
class TestIsIMAModel:
    @pytest.mark.parametrize("model,expected", [
        ("ima-knowledge-qa", True),
        ("ima-notes-search", True),
        ("ima-notes-create", True),
        ("ima-custom", True),
        ("claude-3.7", False),
        ("gpt-5", False),
        ("auto-chat", False),
        ("", False),
        (None, False),
    ])
    def test_model_detection(self, model, expected):
        assert is_ima_model(model) is expected


class TestIMAListModels:
    def test_models_listed(self):
        models = ima_list_models()
        ids = {m["id"] for m in models}
        assert ids == set(IMA_MODEL_IDS.keys())
        for m in models:
            assert m["owned_by"] == "ima"
            assert m["object"] == "model"


# -------------------- 消息提取 --------------------
class TestExtractUserMessage:
    def test_simple_string(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert _extract_last_user_message(msgs) == "hello"

    def test_last_user_wins(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        assert _extract_last_user_message(msgs) == "second"

    def test_multimodal_text_parts(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "part1 "},
                {"type": "image_url", "image_url": {}},
                {"type": "text", "text": "part2"},
            ],
        }]
        assert _extract_last_user_message(msgs) == "part1 part2"

    def test_no_user_message(self):
        msgs = [{"role": "system", "content": "you are a helper"}]
        assert _extract_last_user_message(msgs) == ""

    def test_strips_whitespace(self):
        msgs = [{"role": "user", "content": "  hello  \n"}]
        assert _extract_last_user_message(msgs) == "hello"


# -------------------- 结果格式化 --------------------
class TestFormatResults:
    def test_knowledge_context(self):
        results = [
            {"title": "Doc1", "content": "snippet 1", "url": "https://x"},
            {"name": "Doc2", "snippet": "snippet 2"},
        ]
        out = _format_results_as_context(results)
        assert "[1] Doc1" in out
        assert "snippet 1" in out
        assert "[2] Doc2" in out

    def test_knowledge_context_empty(self):
        assert _format_results_as_context([]) == ""

    def test_notes_format(self):
        docs = [{"title": "Note1", "content": "abc"}]
        out = _format_results_for_notes(docs)
        assert "Note1" in out
        assert "abc" in out

    def test_notes_format_empty(self):
        assert _format_results_for_notes([]) == ""


# -------------------- 端到端：FastAPI TestClient --------------------
@pytest.fixture
def patched_ima_client(monkeypatch):
    """用一个 mock IMAClient 替换 app.state.ima_client 与 get_ima_client"""
    from src import ima_router

    class FakeClient:
        def __init__(self):
            self.base_url = "https://ima.qq.com"
            self.client_id = "test_cid"
            self.api_key = "test_key"
            self.calls = []

        async def list_knowledge_bases(self):
            self.calls.append(("list_knowledge_bases",))
            return [{"id": "kb1", "name": "Demo KB"}]

        async def list_docs(self, limit=20, cursor=None):
            self.calls.append(("list_docs", limit, cursor))
            return {"docs": [], "next_cursor": None}

        async def search_knowledge(self, query, knowledge_base_id=None, top_k=5):
            self.calls.append(("search_knowledge", query, knowledge_base_id, top_k))
            return [
                {"title": "R1", "content": "result-1", "url": "https://example.com/r1"},
                {"title": "R2", "snippet": "result-2"},
            ]

        async def search_docs(self, query, limit=10):
            self.calls.append(("search_docs", query, limit))
            return [
                {"title": "Note A", "content": "abc"},
                {"title": "Note B", "snippet": "xyz"},
            ]

        async def import_doc(self, title, content, content_format=1):
            self.calls.append(("import_doc", title, content, content_format))
            return "note-xyz"

        async def append_doc(self, note_id, content, content_format=1):
            self.calls.append(("append_doc", note_id, content, content_format))
            return True

        async def get_doc_content(self, note_id):
            self.calls.append(("get_doc_content", note_id))
            return "full content"

        async def get_media_info(self, media_id):
            self.calls.append(("get_media_info", media_id))
            return {"media_id": media_id, "title": "M1"}

        async def aclose(self):
            pass

    fake = FakeClient()
    monkeypatch.setattr(ima_router, "get_ima_client", lambda: fake)
    return fake


@pytest.fixture
def app(patched_ima_client):
    app = FastAPI()
    app.include_router(ima_router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestStandaloneREST:
    def test_health(self, client):
        resp = client.get("/ima/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["configured"] is True
        assert "ima-knowledge-qa" in body["models"]

    def test_list_knowledge_bases(self, client, patched_ima_client):
        resp = client.get("/ima/knowledge/bases")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["knowledge_bases"][0]["id"] == "kb1"
        assert ("list_knowledge_bases",) in patched_ima_client.calls

    def test_search_knowledge(self, client, patched_ima_client):
        resp = client.post("/ima/knowledge/search", json={"query": "hi", "top_k": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["query"] == "hi"
        assert ("search_knowledge", "hi", None, 2) in patched_ima_client.calls

    def test_search_knowledge_validation(self, client):
        # 缺 query
        resp = client.post("/ima/knowledge/search", json={"top_k": 2})
        assert resp.status_code == 422

    def test_get_media_info(self, client, patched_ima_client):
        resp = client.get("/ima/knowledge/media/abc")
        assert resp.status_code == 200
        assert ("get_media_info", "abc") in patched_ima_client.calls

    def test_list_notes(self, client, patched_ima_client):
        resp = client.get("/ima/notes?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        # FakeClient 调用 list_docs 返回空 dict（router 透传给 data）
        assert "data" in body

    def test_search_notes(self, client, patched_ima_client):
        resp = client.get("/ima/notes/search?q=hi")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["query"] == "hi"
        assert ("search_docs", "hi", 10) in patched_ima_client.calls

    def test_create_note(self, client, patched_ima_client):
        resp = client.post(
            "/ima/notes", json={"title": "t", "content": "c", "content_format": 1}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["note_id"] == "note-xyz"

    def test_create_note_validation(self, client):
        resp = client.post("/ima/notes", json={"title": "", "content": "c"})
        assert resp.status_code == 422

    def test_append_note(self, client, patched_ima_client):
        resp = client.post(
            "/ima/notes/append",
            json={"note_id": "n1", "content": "more"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True

    def test_get_note_content(self, client, patched_ima_client):
        resp = client.get("/ima/notes/note-1/content")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["content"] == "full content"


# -------------------- 凭证缺失 --------------------
class TestMissingCredentials:
    @pytest.fixture
    def app_no_creds(self, monkeypatch):
        from src.ima_router import router as _router, get_ima_client as _get_client

        class NoCredsClient:
            base_url = "https://ima.qq.com"
            client_id = None
            api_key = None

        # patch 模块级函数
        import src.ima_router as _mod
        monkeypatch.setattr(_mod, "get_ima_client", lambda: NoCredsClient())
        app = FastAPI()
        app.include_router(_router)
        return app

    def test_search_returns_503(self, app_no_creds):
        with TestClient(app_no_creds) as c:
            resp = c.get("/ima/knowledge/bases")
            assert resp.status_code == 503
            assert "凭证" in resp.json()["detail"]

    def test_health_degraded(self, app_no_creds):
        with TestClient(app_no_creds) as c:
            resp = c.get("/ima/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "degraded"
            assert body["configured"] is False


# -------------------- 业务错误映射 --------------------
class TestBusinessErrorMapping:
    @pytest.fixture
    def app_business_error(self, monkeypatch):
        from src.ima_router import router as _router

        class ErrorClient:
            base_url = "https://ima.qq.com"
            client_id = "c"
            api_key = "k"

            async def search_knowledge(self, query, **kwargs):
                raise IMAError(code=10001, msg="参数非法")

            async def list_knowledge_bases(self):
                raise IMAError(code=10002, msg="凭证无效")

            async def search_docs(self, query, limit=10):
                raise IMAError(code=10003, msg="权限不足")

            async def get_doc_content(self, note_id):
                raise IMAError(code=10004, msg="资源不存在")

            async def import_doc(self, title, content, content_format=1):
                raise IMAError(code=10005, msg="请求过于频繁")

        import src.ima_router as _mod
        monkeypatch.setattr(_mod, "get_ima_client", lambda: ErrorClient())
        app = FastAPI()
        app.include_router(_router)
        return app

    def test_10001_maps_to_400(self, app_business_error):
        with TestClient(app_business_error) as c:
            resp = c.post("/ima/knowledge/search", json={"query": "x"})
            assert resp.status_code == 400

    def test_10002_maps_to_401(self, app_business_error):
        with TestClient(app_business_error) as c:
            resp = c.get("/ima/knowledge/bases")
            assert resp.status_code == 401

    def test_10003_maps_to_403(self, app_business_error):
        with TestClient(app_business_error) as c:
            resp = c.get("/ima/notes/search?q=x")
            assert resp.status_code == 403

    def test_10004_maps_to_404(self, app_business_error):
        with TestClient(app_business_error) as c:
            resp = c.get("/ima/notes/note-1/content")
            assert resp.status_code == 404

    def test_10005_maps_to_429(self, app_business_error):
        with TestClient(app_business_error) as c:
            resp = c.post("/ima/notes", json={"title": "t", "content": "c"})
            assert resp.status_code == 429


# -------------------- OpenAI 兼容代理 --------------------
@pytest.mark.asyncio
async def test_openai_chat_knowledge_qa(monkeypatch):
    from src import ima_router

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

        async def search_knowledge(self, query, **kwargs):
            return [
                {"title": "A", "content": "alpha", "url": "https://x"},
                {"title": "B", "snippet": "beta"},
            ]

    monkeypatch.setattr(ima_router, "get_ima_client", lambda: FakeClient())

    result = await ima_router.ima_openai_chat_completions({
        "model": "ima-knowledge-qa",
        "messages": [{"role": "user", "content": "What is X?"}],
        "stream": False,
    })
    assert result["object"] == "chat.completion"
    assert result["model"] == "ima-knowledge-qa"
    content = result["choices"][0]["message"]["content"]
    assert "[1] A" in content
    assert "alpha" in content
    assert "[2] B" in content


@pytest.mark.asyncio
async def test_openai_chat_notes_search(monkeypatch):
    from src import ima_router

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

        async def search_docs(self, query, limit=10):
            return [{"title": "N1", "content": "xx"}]

    monkeypatch.setattr(ima_router, "get_ima_client", lambda: FakeClient())

    result = await ima_router.ima_openai_chat_completions({
        "model": "ima-notes-search",
        "messages": [{"role": "user", "content": "find note"}],
    })
    content = result["choices"][0]["message"]["content"]
    assert "N1" in content
    assert "xx" in content


@pytest.mark.asyncio
async def test_openai_chat_notes_create(monkeypatch):
    from src import ima_router

    captured = {}

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

        async def import_doc(self, title, content, content_format=1):
            captured["title"] = title
            captured["content"] = content
            captured["format"] = content_format
            return "nid-999"

    monkeypatch.setattr(ima_router, "get_ima_client", lambda: FakeClient())

    result = await ima_router.ima_openai_chat_completions({
        "model": "ima-notes-create",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        "metadata": {"title": "My Note"},
    })
    assert captured["title"] == "My Note"
    assert "[user] hello" in captured["content"]
    assert "nid-999" in result["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_openai_chat_stream_returns_sse_dict(monkeypatch):
    from src import ima_router

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

        async def search_knowledge(self, query, **kwargs):
            return [{"title": "A", "content": "x"}]

    monkeypatch.setattr(ima_router, "get_ima_client", lambda: FakeClient())
    result = await ima_router.ima_openai_chat_completions({
        "model": "ima-knowledge-qa",
        "messages": [{"role": "user", "content": "q"}],
        "stream": True,
    })
    assert result["object"] == "chat.completion.chunk"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "content" in result["choices"][0]["delta"]


@pytest.mark.asyncio
async def test_openai_chat_no_user_message_raises():
    from src import ima_router
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await ima_router.ima_openai_chat_completions({
            "model": "ima-knowledge-qa",
            "messages": [{"role": "system", "content": "you are a helper"}],
        })
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_openai_chat_unsupported_model_raises():
    from src import ima_router
    from fastapi import HTTPException

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

    ima_router.get_ima_client = lambda: FakeClient()

    with pytest.raises(HTTPException) as exc_info:
        await ima_router.ima_openai_chat_completions({
            "model": "ima-unknown",
            "messages": [{"role": "user", "content": "q"}],
        })
    assert exc_info.value.status_code == 400
    assert "不支持的 IMA 模型" in exc_info.value.detail


@pytest.mark.asyncio
async def test_openai_chat_business_error_propagates(monkeypatch):
    from src import ima_router
    from fastapi import HTTPException

    class FakeClient:
        client_id = "c"
        api_key = "k"
        base_url = "https://ima.qq.com"

        async def search_knowledge(self, query, **kwargs):
            raise IMAError(code=10001, msg="参数非法")

    monkeypatch.setattr(ima_router, "get_ima_client", lambda: FakeClient())

    with pytest.raises(HTTPException) as exc_info:
        await ima_router.ima_openai_chat_completions({
            "model": "ima-knowledge-qa",
            "messages": [{"role": "user", "content": "q"}],
        })
    assert exc_info.value.status_code == 400
