"""
IMA (腾讯智能助手) 路由层

提供三组能力：

1. 独立 REST 接口（/ima/*）—— 知识库检索 / 笔记管理
2. OpenAI 兼容代理（/v1/chat/completions 中以 ima- 开头的模型）
3. 在 /v1/models 中声明 IMA 相关模型 ID

所有对外接口都通过 IMAClient 与 IMA 官方 OpenAPI 通信。
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .ima_client import IMAClient, IMAError, get_ima_client

logger = logging.getLogger(__name__)

router = APIRouter()

# 通过 OpenAI 协议访问 IMA 的模型 ID 前缀
IMA_MODEL_PREFIX = "ima-"
# 预声明的 IMA 模型 ID（用于 /v1/models 与 /v1/chat/completions 路由分发）
IMA_MODEL_IDS = {
    "ima-knowledge-qa": "基于 IMA 知识库的检索问答",
    "ima-notes-search": "检索 IMA 笔记",
    "ima-notes-create": "通过 IMA 创建笔记",
}


# -------------------- Pydantic 模型（独立 REST 接口） --------------------
class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    knowledge_base_id: Optional[str] = None
    top_k: int = Field(5, ge=1, le=20)


class NoteCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    content_format: int = Field(1, ge=1, le=2)


class NoteAppendRequest(BaseModel):
    note_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    content_format: int = Field(1, ge=1, le=2)


class IMAStandardResponse(BaseModel):
    success: bool = True
    data: Optional[Any] = None
    error: Optional[str] = None


# -------------------- 工具函数 --------------------
def _normalize_business_error(exc: IMAError) -> HTTPException:
    """将 IMA 业务错误映射为 HTTPException"""
    if exc.code in (10001,):
        return HTTPException(status_code=400, detail=f"参数错误: {exc.msg}")
    if exc.code in (10002,):
        return HTTPException(status_code=401, detail=f"凭证无效: {exc.msg}")
    if exc.code in (10003,):
        return HTTPException(status_code=403, detail=f"权限不足: {exc.msg}")
    if exc.code in (10004,):
        return HTTPException(status_code=404, detail=f"资源不存在: {exc.msg}")
    if exc.code in (10005,):
        return HTTPException(status_code=429, detail=f"请求过于频繁: {exc.msg}")
    if exc.code >= 10100 or exc.code < 0:
        return HTTPException(status_code=502, detail=f"IMA 上游错误: {exc.msg}")
    return HTTPException(status_code=500, detail=f"IMA 业务错误 [{exc.code}]: {exc.msg}")


async def _client_from_request(request: Request) -> IMAClient:
    """从 FastAPI Request 取出 IMA 客户端，缺失凭证时抛出 503"""
    client: Optional[IMAClient] = getattr(request.app.state, "ima_client", None)
    if client is None:
        client = get_ima_client()
        request.app.state.ima_client = client
    if not client.client_id or not client.api_key:
        raise HTTPException(
            status_code=503,
            detail="IMA 凭证未配置：请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 环境变量",
        )
    return client


# -------------------- 独立 REST 接口 --------------------
@router.get("/ima/health", summary="IMA 模块健康检查")
async def ima_health() -> Dict[str, Any]:
    """健康检查端点；不消耗 IMA 配额"""
    client = get_ima_client()
    configured = bool(client.client_id and client.api_key)
    return {
        "status": "ok" if configured else "degraded",
        "configured": configured,
        "base_url": client.base_url,
        "models": list(IMA_MODEL_IDS.keys()),
    }


@router.get(
    "/ima/knowledge/bases",
    response_model=IMAStandardResponse,
    summary="列出可访问的知识库",
)
async def list_knowledge_bases(request: Request) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        bases = await client.list_knowledge_bases()
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data={"knowledge_bases": bases})


@router.post(
    "/ima/knowledge/search",
    response_model=IMAStandardResponse,
    summary="检索 IMA 知识库",
)
async def search_knowledge(
    payload: KnowledgeSearchRequest, request: Request
) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        results = await client.search_knowledge(
            query=payload.query,
            knowledge_base_id=payload.knowledge_base_id,
            top_k=payload.top_k,
        )
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data={"results": results, "query": payload.query})


@router.get(
    "/ima/knowledge/media/{media_id}",
    response_model=IMAStandardResponse,
    summary="获取知识库条目元信息",
)
async def get_media_info(media_id: str, request: Request) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        info = await client.get_media_info(media_id=media_id)
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data=info)


@router.get("/ima/notes", response_model=IMAStandardResponse, summary="列出笔记")
async def list_notes(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        data = await client.list_docs(limit=limit, cursor=cursor)
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data=data)


@router.get(
    "/ima/notes/search",
    response_model=IMAStandardResponse,
    summary="搜索笔记",
)
async def search_notes(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500, description="搜索关键词"),
    limit: int = Query(10, ge=1, le=50),
) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        docs = await client.search_docs(query=q, limit=limit)
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data={"docs": docs, "query": q})


@router.post("/ima/notes", response_model=IMAStandardResponse, summary="创建笔记")
async def create_note(payload: NoteCreateRequest, request: Request) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        note_id = await client.import_doc(
            title=payload.title,
            content=payload.content,
            content_format=payload.content_format,
        )
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IMAStandardResponse(data={"note_id": note_id})


@router.post(
    "/ima/notes/append",
    response_model=IMAStandardResponse,
    summary="向笔记追加内容",
)
async def append_note(payload: NoteAppendRequest, request: Request) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        success = await client.append_doc(
            note_id=payload.note_id,
            content=payload.content,
            content_format=payload.content_format,
        )
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IMAStandardResponse(data={"success": success})


@router.get(
    "/ima/notes/{note_id}/content",
    response_model=IMAStandardResponse,
    summary="获取笔记正文",
)
async def get_note_content(note_id: str, request: Request) -> IMAStandardResponse:
    client = await _client_from_request(request)
    try:
        content = await client.get_doc_content(note_id=note_id)
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc
    return IMAStandardResponse(data={"note_id": note_id, "content": content})


# -------------------- OpenAI 兼容代理 --------------------
def is_ima_model(model_name: Optional[str]) -> bool:
    """判定是否属于 IMA 模型"""
    if not model_name:
        return False
    return model_name.startswith(IMA_MODEL_PREFIX) or model_name in IMA_MODEL_IDS


def _extract_last_user_message(messages: List[Dict[str, Any]]) -> str:
    """从 OpenAI 消息列表中提取最后一条 user 文本"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                # 多模态/分段时拼接 text 段
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                if parts:
                    return "".join(parts).strip()
    return ""


def _format_results_as_context(results: List[Dict[str, Any]]) -> str:
    """将 IMA 检索结果拼接为模型可直接引用的上下文"""
    if not results:
        return ""
    blocks: List[str] = []
    for idx, item in enumerate(results, start=1):
        title = item.get("title") or item.get("name") or f"参考 {idx}"
        snippet = item.get("content") or item.get("snippet") or item.get("text") or ""
        url = item.get("url")
        header = f"[{idx}] {title}"
        if url:
            header += f" ({url})"
        blocks.append(f"{header}\n{snippet}".strip())
    return "\n\n".join(blocks)


def _format_results_for_notes(docs: List[Dict[str, Any]]) -> str:
    """格式化笔记检索结果"""
    if not docs:
        return ""
    blocks: List[str] = []
    for idx, doc in enumerate(docs, start=1):
        title = doc.get("title") or f"笔记 {idx}"
        snippet = doc.get("content") or doc.get("snippet") or ""
        blocks.append(f"[{idx}] {title}\n{snippet}".strip())
    return "\n\n".join(blocks)


async def ima_openai_chat_completions(
    request_body: Dict[str, Any], request: Optional[Request] = None
) -> Dict[str, Any]:
    """在 OpenAI 协议入口处理 ima-* 模型的请求

    由 codebuddy_router 在检测到 ima- 前缀模型时调用。
    """
    model = request_body.get("model", "")
    messages = request_body.get("messages", [])
    stream = bool(request_body.get("stream", False))

    last_user_text = _extract_last_user_message(messages)
    if not last_user_text:
        raise HTTPException(
            status_code=400,
            detail="ima-* 模型需要至少一条 user 消息且 content 为字符串",
        )

    client: Optional[IMAClient] = None
    if request is not None:
        client = getattr(request.app.state, "ima_client", None)
    if client is None:
        client = get_ima_client()
    if not client.client_id or not client.api_key:
        raise HTTPException(
            status_code=503,
            detail="IMA 凭证未配置：请设置 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 环境变量",
        )

    try:
        if model == "ima-knowledge-qa":
            data_section = await client.search_knowledge(query=last_user_text)
            context = _format_results_as_context(data_section)
            answer = (
                f"根据 IMA 知识库检索结果：\n\n{context}"
                if context
                else "未在 IMA 知识库中检索到相关内容。"
            )
        elif model == "ima-notes-search":
            data_section = await client.search_docs(query=last_user_text)
            context = _format_results_for_notes(data_section)
            answer = (
                f"IMA 笔记检索结果：\n\n{context}"
                if context
                else "未在 IMA 笔记中检索到相关内容。"
            )
        elif model == "ima-notes-create":
            # 把对话内容当笔记创建
            note_title = request_body.get("metadata", {}).get("title") or "CodeBuddy2API 笔记"
            history = [
                f"[{msg.get('role')}] {msg.get('content')}"
                for msg in messages
                if isinstance(msg.get("content"), str)
            ]
            note_body = "\n".join(history)
            note_id = await client.import_doc(title=note_title, content=note_body)
            answer = f"已创建 IMA 笔记，note_id={note_id}"
        else:
            raise HTTPException(status_code=400, detail=f"不支持的 IMA 模型: {model}")
    except IMAError as exc:
        raise _normalize_business_error(exc) from exc

    if stream:
        return _wrap_text_as_stream(answer, model=model)

    return _wrap_text_as_completion(answer, model=model)


def _wrap_text_as_completion(text: str, model: str) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-ima-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _wrap_text_as_stream(text: str, model: str) -> Dict[str, Any]:
    """流式输出时返回的伪 SSE 字典（由调用方序列化为 SSE）"""
    return {
        "id": f"chatcmpl-ima-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


def ima_list_models() -> List[Dict[str, Any]]:
    """在 /v1/models 中追加 IMA 模型 ID"""
    ts = int(time.time())
    return [
        {
            "id": model_id,
            "object": "model",
            "created": ts,
            "owned_by": "ima",
        }
        for model_id in IMA_MODEL_IDS.keys()
    ]


# -------------------- 启动 / 关闭钩子 --------------------
async def ima_startup(app) -> None:
    """在 FastAPI lifespan 中调用，预热 IMA HTTP 连接池"""
    client = get_ima_client()
    app.state.ima_client = client
    logger.info("IMA 路由启动（凭证已配置: %s）", bool(client.client_id and client.api_key))


async def ima_shutdown(app) -> None:
    """关闭 IMA HTTP 连接池"""
    client: Optional[IMAClient] = getattr(app.state, "ima_client", None)
    if client is not None:
        await client.aclose()
        logger.info("IMA 路由已关闭")
