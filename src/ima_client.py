"""
IMA (腾讯智能助手) OpenAPI 客户端

封装 IMA 知识库和笔记相关 OpenAPI 调用，提供：
- 知识库检索（search / list / get）
- 笔记管理（list / create / append / search）
- 错误处理与重试

凭证来源（按优先级）：
1. 环境变量 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
2. 配置文件 ~/.config/ima/client_id / ~/.config/ima/api_key
3. CodeBuddy2api 配置（通过 config 模块读取）

所有接口仅在官方 Base URL（https://ima.qq.com）发起请求，凭证仅作为 HTTP Header
发送，不写入日志或落盘。
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

IMA_BASE_URL = "https://ima.qq.com"

# 业务错误码 -> 默认提示
_DEFAULT_ERROR_MESSAGES = {
    -1: "未知错误",
    10001: "参数非法",
    10002: "凭证无效",
    10003: "权限不足",
    10004: "资源不存在",
    10005: "请求频率超限",
    10100: "服务内部错误",
}


class IMAError(Exception):
    """IMA 业务错误（code != 0 的后端响应）"""

    def __init__(self, code: int, msg: str, request_id: Optional[str] = None):
        self.code = code
        self.msg = msg
        self.request_id = request_id
        super().__init__(f"IMAError code={code} msg={msg}")


class IMAClient:
    """IMA OpenAPI 异步客户端

    使用示例::

        client = IMAClient()
        result = await client.search_knowledge(query="什么是 CodeBuddy")
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: str = IMA_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

        cid, key = self._resolve_credentials(client_id, api_key)
        self.client_id = cid
        self.api_key = key

        self._client: Optional[httpx.AsyncClient] = None

    # -------------------- 凭证解析 --------------------
    @staticmethod
    def _resolve_credentials(
        client_id: Optional[str], api_key: Optional[str]
    ) -> tuple[Optional[str], Optional[str]]:
        """按优先级解析凭证：参数 -> 环境变量 -> 配置文件 -> config 模块"""
        if client_id and api_key:
            return client_id, api_key

        env_cid = os.getenv("IMA_OPENAPI_CLIENTID")
        env_key = os.getenv("IMA_OPENAPI_APIKEY")
        if env_cid and env_key:
            return env_cid, env_key

        # 配置文件 ~/.config/ima/{client_id,api_key}
        # Windows 上 Path.home() 使用 USERPROFILE，需同时尊重 HOME
        home_dir = Path(os.getenv("HOME") or Path.home())
        config_dir = home_dir / ".config" / "ima"
        cid_path = config_dir / "client_id"
        key_path = config_dir / "api_key"
        if cid_path.exists() and key_path.exists():
            try:
                return cid_path.read_text(encoding="utf-8").strip(), key_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass

        # 最后尝试 CodeBuddy2api 自身配置（运行时注入）
        try:
            from config import get_ima_client_id, get_ima_api_key  # type: ignore
            return get_ima_client_id(), get_ima_api_key()
        except Exception:  # pragma: no cover - 兼容性兜底
            return None, None

    # -------------------- HTTP 客户端 --------------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                headers={"User-Agent": "codebuddy2api/ima-integration"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "IMAClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # -------------------- 核心调用 --------------------
    def _build_headers(self) -> Dict[str, str]:
        if not self.client_id or not self.api_key:
            raise IMAError(-1, "IMA 凭证未配置（client_id / api_key 缺失）")
        return {
            "X-IMA-ClientId": self.client_id,
            "X-IMA-ApiKey": self.api_key,
            "Content-Type": "application/json; charset=utf-8",
        }

    async def _request(
        self,
        api_path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """统一 POST + JSON 调用，处理重试与业务错误

        后端业务错误通过抛出 IMAError 暴露，便于上层做兜底。
        """
        if not api_path.startswith("/"):
            api_path = "/" + api_path

        payload = payload or {}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._build_headers()
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= self.max_retries:
            attempt += 1
            client = await self._get_client()
            try:
                resp = await client.post(api_path, content=body, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    f"[IMA] 网络异常 (attempt {attempt}/{self.max_retries + 1}): {exc}"
                )
                if attempt > self.max_retries:
                    raise IMAError(-1, f"网络错误: {exc}") from exc
                await self._sleep_backoff(attempt)
                continue

            # 非 2xx：网络层错误，触发重试
            if resp.status_code >= 500:
                last_exc = IMAError(resp.status_code, f"上游返回 HTTP {resp.status_code}")
                logger.warning(
                    f"[IMA] 上游 5xx (attempt {attempt}/{self.max_retries + 1}): "
                    f"{resp.status_code} {resp.text[:200]}"
                )
                if attempt > self.max_retries:
                    raise last_exc
                await self._sleep_backoff(attempt)
                continue

            # 尝试解析 JSON
            try:
                data = resp.json()
            except json.JSONDecodeError as exc:
                raise IMAError(
                    -1,
                    f"IMA 返回非 JSON 响应: status={resp.status_code} body={resp.text[:200]!r}",
                ) from exc

            # 业务错误：直接抛出，不重试
            code = data.get("code", 0)
            if code != 0:
                msg = data.get("msg") or _DEFAULT_ERROR_MESSAGES.get(code, "业务错误")
                raise IMAError(code=code, msg=msg, request_id=data.get("request_id"))

            return data.get("data", {}) if isinstance(data, dict) else {}

        # 理论不会到达这里；保险起见
        raise IMAError(-1, f"IMA 调用失败: {last_exc}")

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        # 简单指数退避：0.5s, 1s, 2s ...
        import asyncio

        await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 3.0))

    # -------------------- 知识库 API --------------------
    async def list_knowledge_bases(self) -> List[Dict[str, Any]]:
        """获取可添加的知识库列表"""
        data = await self._request("openapi/list_knowledge_bases", {})
        return data.get("knowledge_bases", []) if isinstance(data, dict) else []

    async def search_knowledge(
        self,
        query: str,
        knowledge_base_id: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """检索知识库

        Args:
            query: 检索关键词
            knowledge_base_id: 限定知识库，为空则检索全部可访问知识库
            top_k: 返回条数
        """
        payload: Dict[str, Any] = {"query": query, "top_k": top_k}
        if knowledge_base_id:
            payload["knowledge_base_id"] = knowledge_base_id
        data = await self._request("openapi/search_knowledge", payload)
        return data.get("results", []) if isinstance(data, dict) else []

    async def get_media_info(self, media_id: str) -> Dict[str, Any]:
        """获取知识库条目元信息"""
        data = await self._request(
            "openapi/get_media_info", {"media_id": media_id}
        )
        return data if isinstance(data, dict) else {}

    # -------------------- 笔记 API --------------------
    async def list_docs(self, limit: int = 20, cursor: Optional[str] = None) -> Dict[str, Any]:
        """列出笔记"""
        payload: Dict[str, Any] = {"limit": limit}
        if cursor:
            payload["cursor"] = cursor
        return await self._request("openapi/list_docs", payload)

    async def search_docs(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """搜索笔记"""
        data = await self._request(
            "openapi/search_docs", {"query": query, "limit": limit}
        )
        return data.get("docs", []) if isinstance(data, dict) else []

    async def import_doc(self, title: str, content: str, content_format: int = 1) -> str:
        """创建笔记并返回 note_id

        Args:
            content_format: 1 = 纯文本，2 = Markdown
        """
        if not isinstance(title, str) or not title:
            raise ValueError("title 必填且必须为字符串")
        if not isinstance(content, str) or not content:
            raise ValueError("content 必填且必须为字符串")

        data = await self._request(
            "openapi/import_doc",
            {
                "title": title,
                "content": content,
                "content_format": content_format,
            },
        )
        note_id = data.get("note_id") or data.get("id") if isinstance(data, dict) else None
        if not note_id:
            raise IMAError(-1, "IMA import_doc 未返回 note_id")
        return str(note_id)

    async def append_doc(
        self, note_id: str, content: str, content_format: int = 1
    ) -> bool:
        """向已有笔记追加内容"""
        if not note_id:
            raise ValueError("note_id 必填")
        if not isinstance(content, str) or not content:
            raise ValueError("content 必填且必须为字符串")

        data = await self._request(
            "openapi/append_doc",
            {
                "note_id": note_id,
                "content": content,
                "content_format": content_format,
            },
        )
        return bool(data.get("success", True)) if isinstance(data, dict) else True

    async def get_doc_content(self, note_id: str) -> str:
        """获取笔记正文"""
        data = await self._request(
            "openapi/get_doc_content", {"note_id": note_id}
        )
        return data.get("content", "") if isinstance(data, dict) else ""


# -------------------- 单例 --------------------
_default_client: Optional[IMAClient] = None


def get_ima_client() -> IMAClient:
    """获取默认 IMA 客户端（延迟初始化，避免无凭证场景阻塞）"""
    global _default_client
    if _default_client is None:
        _default_client = IMAClient()
    return _default_client


def reset_ima_client() -> None:
    """重置默认客户端（用于凭证热更新）"""
    global _default_client
    _default_client = None
