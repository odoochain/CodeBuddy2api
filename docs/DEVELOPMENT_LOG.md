# 开发记录 · 可靠性与 OpenAI 兼容补齐

> 本文档汇总最近一轮（feature/codebuddy-reliability 分支）的工作成果、关键决策、踩过的坑以及后续计划。

## 一、本轮目标

在不引入 IMA 的前提下，**优先完善原有程序**：
1. 可靠性与重试机制
2. 性能与高并发优化
3. OpenAI 兼容接口补齐

分支：`feature/codebuddy-reliability`（已推送至 origin）。

## 二、已完成内容

### 1. 可靠性工具模块（`src/reliability.py`）

- **错误分类** `classify_exception`
  - 网络异常（timeout、ConnectError、ReadError）→ `RETRYABLE_NETWORK`
  - HTTP 5xx / 408 / 425 / 429 → `RETRYABLE_SERVER`
  - HTTP 4xx（除 408/429）→ `NON_RETRYABLE_CLIENT`，立即抛出
- **指数退避重试** `call_with_retry` / `@async_retry`
  - 默认 `max_retries=3`，初始 `0.5s`、上限 `8s`、倍数 `2.0`、抖动 `±25%`
  - 仅对 `RETRYABLE_*` 分类执行重试；客户端错误立即终止
- **熔断器** `CircuitBreaker`（CLOSED / OPEN / HALF_OPEN）
  - 默认阈值 `5` 次失败、恢复窗口 `30s`
  - OPEN 状态直接 `CircuitOpenError`，由路由层翻译为 HTTP 503
- **并发限流** `ConcurrencyLimiter`（基于 `asyncio.Semaphore`）
  - 默认 64 路并发，可通过 `CODEBUDDY_MAX_CONCURRENT` 调优
- **TTL 缓存** `TTLCache`
  - 默认 60s 过期，用于 `/v1/models` 元数据接口减压

### 2. `codebuddy_router.py` 集成

- HTTP 客户端连接池参数化（`CODEBUDDY_KEEPALIVE`、`CODEBUDDY_MAX_CONNECTIONS`、`CODEBUDDY_TIMEOUT`）
- `chat_completions` 入口：限流 + 熔断 + 重试三层保护
- `/v1/models`：`TTLCache` 缓存
- 新增 `GET /v1/models/{model_id}` 和 `GET /v1/health`（返回限流器 / 熔断器运行时快照）

### 3. OpenAI 兼容补齐

- `RequestProcessor.prepare_payload`：
  - `response_format` 归一化（`str` → `dict`），`json_object` / `json_schema` 自动追加 system 提示
  - 显式 OpenAI 透传白名单（temperature、max_tokens、max_completion_tokens、top_p、stop、seed、tools、tool_choice 等），未知字段直接剔除避免上游拒收
- `POST /v1/completions`：
  - `prompt`（str 或 list）→ messages，复用 chat 通道的鉴权 / 限流 / 熔断 / 重试
  - 流式 / 非流式均支持；SSE chunk 实时重塑为 `text_completion` 形态
- `POST /v1/embeddings`：
  - CodeBuddy 不提供 embedding，返回 OpenAI 风格 501 错误（`type=unsupported_request, code=model_not_supported`），便于 SDK 解析

### 4. 测试

- 新增 `tests/test_reliability.py`：错误分类、重试、熔断、限流、缓存全套覆盖
- 新增 `tests/test_openai_compat.py`：24 个用例覆盖 response_format、参数透传、completions、embeddings
- 整合命令：`pytest tests/ -v` → **55 passed**

## 三、关键决策与经验

| 主题 | 决策 | 理由 |
| --- | --- | --- |
| 重试范围 | 仅重试网络类与服务端类 | 客户端错误重试无意义且浪费资源 |
| 熔断与重试协同 | 熔断打开期间 `call_with_retry` 立即抛 `CircuitOpenError` | 避免在熔断窗口内继续发送请求 |
| 透传字段管理 | 显式白名单而非 `**kwargs` 全透 | 上游对未知字段不友好；白名单可控 |
| response_format | 字符串自动展开 + 自动追加 system 提示 | 兼容 OpenAI v1 写法（`"json_object"`）与 SDK dict 写法 |
| completions 重塑 | 后端转 chat + 响应整形 | CodeBuddy 仅有 chat 能力，避免重复实现 |
| embeddings 行为 | 返回 501 而非 404 | OpenAI 客户端 SDK 对 5xx 与 4xx 处理路径不同；501 表明「接口存在但暂不支持」 |

## 四、踩过的坑

- **GitButler remote HEAD 丢失**：仓库被重置后 `refs/remotes/origin/HEAD` 缺失，`but setup` 报 `No HEAD reference found for remote origin`。已通过 `git remote set-head origin main` + `git fetch origin main` 部分恢复；如仍异常可执行 `git remote set-head origin -d` 后重试。
- **httpx 与 starlette.testclient 弃用警告**：仅 warning，不影响功能。后续可替换为 `httpx2`。
- **Windows 平台 Path.home()**：使用 `USERPROFILE` 而非 `HOME`，与 Linux/macOS 不一致。IMA 客户端在 `src/ima_client.py` 中已做兼容。
- **SSE 重塑边界**：必须保留 `data: [DONE]` 与以 `:` 开头的注释行原样转发。

## 五、性能基线与建议

- 默认并发 64：可由 `CODEBUDDY_MAX_CONCURRENT` 调整，建议在 32–128 之间
- 默认连接池：`keepalive_expiry=30`，`max_connections=100`，`max_keepalive_connections=20`
- `/v1/models` 缓存 60s：避免成为热点；如需更短可改环境变量
- 熔断恢复窗口 30s：典型 5xx 突发场景的合理默认

## 六、后续计划

### 短期（同一分支）
- [ ] 给 `/v1/completions` 增加 fixtures 测试（mock 上游 SSE）
- [ ] 给 `chat_completions` 流式路径加上同样的 `call_with_retry`（目前只在非流式上）
- [ ] 暴露 Prometheus 指标端点 `/metrics`（基于限流器 / 熔断器快照）

### 中期（独立分支）
- [ ] IMA 集成：使用 GitButler 虚拟分支隔离（暂不合并到本分支）
- [ ] 凭证管理增强：按模型路由不同凭证
- [ ] 引入 `httpx2` + `pytest-asyncio` 全异步测试

### 长期
- [ ] 项目名称正式切换为 `xz-copilot-hub`（原 `CodeBuddy2api`），同步 README / USAGE / 部署脚本
- [ ] Web 管理界面改造为 SPA，与后端 REST 接口对齐
- [ ] 单元 + 集成测试覆盖率 ≥ 80%

## 七、常用命令速查

```powershell
# 跑全部测试
.\.venv\Scripts\python.exe -m pytest tests/ -v

# 启动服务
.\xzstart.ps1

# 提交并推送
git add -A
git commit -m "feat(...): ..."
git push origin feature-codebuddy-reliability

# 重新接入 GitButler
git remote set-head origin main
git fetch origin main
but setup
```

## 八、当前分支状态

- 分支：`feature-codebuddy-reliability`
- 远端：`origin/feature-codebuddy-reliability`（已同步）
- 距离 `main` 的提交：2 个（init + 本轮）

```
9570791 init
af23ea5 feat(router): integrate reliability toolkit into codebuddy_router
65eee19 feat(router): OpenAI compat - response_format, completions, embeddings
```
