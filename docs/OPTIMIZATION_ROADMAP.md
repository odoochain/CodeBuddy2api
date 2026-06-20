# xz-copilot-hub 优化路线图

> 基于 `feature-codebuddy-reliability` 分支全量代码审查，2026-06-21 生成。
> 按优先级分档，每项标注涉及文件、问题描述、建议方案和预估工作量。

---

## 一、高优先级（可靠性 / 安全性）

### 1.1 流式路径缺少重试与熔断保护

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py` — `handle_stream_response` (L538-604) |
| **现状** | 非流式路径已接入 `call_with_retry` + `guarded(breaker)`；流式路径仅依赖 `SSEConnectionManager` 的简单重连，未接入 `reliability.py` 的重试策略与熔断器。大部分客户端默认 `stream=True`，即主要流量路径无保护。 |
| **方案** | 在 `stream_core()` 发起 `client.stream()` 前包裹 `guarded(breaker)`，将初始连接阶段纳入熔断统计；SSE 传输中断仍由 `SSEConnectionManager` 处理重连。 |
| **工作量** | ~2h |

### 1.2 认证密码比较存在时序攻击风险

| 项目 | 说明 |
|------|------|
| **文件** | `src/auth.py` (L11-21) |
| **现状** | `token != password` 使用 Python `==` 比较字符串，攻击者可通过响应时间差逐字节猜测密码。另外密码错误返回 403（Forbidden）而非 401（Unauthorized），语义不准确。 |
| **方案** | 改用 `secrets.compare_digest(token, password)`；密码错误返回 401。 |
| **工作量** | ~10min |

### 1.3 CORS 策略全开

| 项目 | 说明 |
|------|------|
| **文件** | `web.py` (L50-56) |
| **现状** | `allow_origins=["*"]` + `allow_credentials=True`，任何域都能携带凭据访问 API。 |
| **方案** | 新增 `CODEBUDDY_CORS_ORIGINS` 配置项（默认 `["http://localhost:*"]`），生产环境需显式配置允许的域名列表。`allow_credentials=True` 时 `allow_origins` 不能为 `["*"]`。 |
| **工作量** | ~30min |

### 1.4 SSL 验证默认关闭

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py` — `SecurityConfig.get_ssl_verify()` (L71-81) |
| **现状** | `CODEBUDDY_SSL_VERIFY` 默认 `false`，`docker-compose.yml` 未覆盖。生产环境可能在无 SSL 验证下运行，存在中间人攻击风险。 |
| **方案** | 反转默认值为 `true`；仅在开发时显式设 `false`。`docker-compose.yml` 的 env 示例中加上 `CODEBUDDY_SSL_VERIFY=true`。 |
| **工作量** | ~15min |

### 1.5 缺少 `.gitignore`，凭证有泄露风险

| 项目 | 说明 |
|------|------|
| **文件** | 项目根目录（无 `.gitignore`） |
| **现状** | `.codebuddy_creds/`（含 bearer token JSON）、`__pycache__/`、`.venv/`、`config/config.json` 均未被忽略，`git status` 中已出现。一旦误提交即为安全事故。 |
| **方案** | 创建 `.gitignore`，至少包含以下条目： |

```gitignore
# 凭证 / 密钥
.codebuddy_creds/
config/config.json
.env

# Python
__pycache__/
*.pyc
.venv/
*.egg-info/

# IDE
.vscode/
.idea/

# 测试 / 缓存
.pytest_cache/
htmlcov/
.coverage
```

| **工作量** | ~5min |

---

## 二、中优先级（可维护性 / 性能）

### 2.1 `codebuddy_router.py` 过于臃肿（~1300 行）

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py` |
| **现状** | 包含 6 个 class（`SecurityConfig`、`OpenAICompatibilityConverter`、`SSEConnectionManager`、`StreamResponseAggregator`、`CodeBuddyStreamService`、`RequestProcessor`、`CredentialManager`）+ 全部路由 + 辅助函数，职责混杂。 |
| **方案** | 拆分为独立模块： |

```
src/
├── codebuddy_router.py        # 仅保留路由定义（~300 行）
├── sse_converter.py            # OpenAICompatibilityConverter + StreamResponseAggregator + parse_sse_line
├── request_processor.py        # RequestProcessor + CredentialManager
├── stream_service.py           # CodeBuddyStreamService + SSEConnectionManager
└── ...
```

| **工作量** | ~3h（纯重构，不改逻辑） |

### 2.2 模块级副作用导致 import 顺序敏感

| 项目 | 说明 |
|------|------|
| **文件** | `config.py:178`、`src/codebuddy_api_client.py:232`、`src/codebuddy_token_manager.py:453` |
| **现状** | `load_config()`、`CodeBuddyAPIClient()`、`CodeBuddyTokenManager()` 均在模块 import 时立即执行，构造函数直接读取配置。导致：(1) import 顺序敏感；(2) 测试时难以 mock/override 配置；(3) 与 FastAPI 的 `lifespan` 生命周期管理不一致。 |
| **方案** | 统一改为延迟初始化（lazy singleton）模式，仿照 `reliability.py` 的 `get_default_limiter()` 写法。所有全局实例通过 `get_xxx()` 函数首次调用时创建。 |
| **工作量** | ~2h |

### 2.3 `convert_openai_to_codebuddy_messages` 疑似死代码

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_api_client.py:24-168` |
| **现状** | 路由层通过 `RequestProcessor.prepare_payload` 处理消息格式，而 `CodeBuddyAPIClient.convert_openai_to_codebuddy_messages` 有 ~170 行消息转换逻辑，需确认是否仍有调用方。 |
| **方案** | 全局搜索 `convert_openai_to_codebuddy_messages` 的引用。若无外部调用，删除该方法。若仍有使用，应合并到 `RequestProcessor` 中避免两套并行逻辑。 |
| **工作量** | ~30min |

### 2.4 路由端点未使用 Pydantic 模型校验

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py` 全部 `@router.post` 端点 |
| **现状** | 所有端点用 `await request.json()` 手动解析 + 手动 `validate_request()`。FastAPI 的核心优势（自动 Pydantic 校验、OpenAPI 文档生成、类型推断）完全没有利用。`src/models.py` 已存在但路由层未使用。 |
| **方案** | 定义 `ChatCompletionRequest`、`CompletionRequest`、`EmbeddingRequest` 等 Pydantic model，路由函数签名改为直接注入（如 `body: ChatCompletionRequest`）。移除手动 `validate_request`。 |
| **工作量** | ~3h |

### 2.5 `validate_and_fix_tool_call_args` JSON 修复逻辑脆弱

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py:295-348` |
| **现状** | 手动追加 `}` / `]` 修复不完整 JSON；失败时静默返回 `'{}'`，工具调用参数被丢弃，下游可能产生难以排查的错误。 |
| **方案** | (1) 失败时记录 `logger.warning` 包含原始参数内容（脱敏后）；(2) 考虑引入 `json-repair` 库做更鲁棒的修复；(3) 返回 `'{}'` 时在响应中附加提示信息。 |
| **工作量** | ~1h |

### 2.6 `HTTP_CLIENT_CONFIG` 在 import 时固化

| 项目 | 说明 |
|------|------|
| **文件** | `src/codebuddy_router.py:111` |
| **现状** | `HTTP_CLIENT_CONFIG = _build_http_client_config()` 在模块加载时执行，之后通过 settings UI 热更新的超时/连接池参数不会生效。与 `config.py` 的热加载设计矛盾。 |
| **方案** | 在 `get_http_client()` 中检测配置变更（比对关键参数 hash），变更时重建客户端。或在 `update_settings()` 回调中触发客户端重建。 |
| **工作量** | ~1.5h |

---

## 三、低优先级（代码质量 / 开发体验）

### 3.1 生产/测试依赖未分离

| 项目 | 说明 |
|------|------|
| **文件** | `requirements.txt` |
| **现状** | `pytest`、`pytest-asyncio` 混在生产依赖中。Docker 镜像会安装不必要的测试框架。 |
| **方案** | 拆分为 `requirements.txt`（生产）+ `requirements-dev.txt`（开发/测试，`-r requirements.txt` 继承生产依赖）。`Dockerfile` 中只安装 `requirements.txt`。 |
| **工作量** | ~15min |

### 3.2 缺少 `Dockerfile`

| 项目 | 说明 |
|------|------|
| **文件** | 项目根目录 |
| **现状** | `docker-compose.yml` 引用 `build: .` 但没有 Dockerfile，无法本地构建镜像。目前依赖预构建的 `sliverkiss/codebuddy2api:latest`。 |
| **方案** | 创建多阶段 Dockerfile，基于 `python:3.13-slim`。 |
| **工作量** | ~30min |

### 3.3 日志语言混杂

| 项目 | 说明 |
|------|------|
| **文件** | 全局（`src/*.py`） |
| **现状** | 日志消息在中英文间切换（"熔断器开启，拒绝请求" vs "Connection failed after N retries"），不利于运维 grep/告警规则编写。 |
| **方案** | 统一为英文日志。中文注释可保留。 |
| **工作量** | ~1h |

### 3.4 缺少 Prometheus 指标端点

| 项目 | 说明 |
|------|------|
| **文件** | 待新增 |
| **现状** | `reliability.py` 已有 `snapshot()` 方法暴露运行时状态，但没有标准 `/metrics` 端点。`/v1/health` 返回的是 JSON 快照，无法被 Prometheus 直接抓取。 |
| **方案** | 引入 `prometheus-fastapi-instrumentator` 或手动暴露 `Counter/Gauge`（请求总数、延迟分位、熔断状态、并发在飞量）。 |
| **工作量** | ~2h |

### 3.5 项目名称未统一

| 项目 | 说明 |
|------|------|
| **文件** | `README.md`、`USAGE.md`、`web.py`、`docker-compose.yml` |
| **现状** | 代码中混用 `CodeBuddy2API`、`codebuddy2api`、`xz-copilot-hub`。DEVELOPMENT_LOG 也提到长期计划切换为 `xz-copilot-hub`。 |
| **方案** | 统一项目对外名称，更新所有引用点。 |
| **工作量** | ~1h |

---

## 四、实施建议

### 推荐分批次实施

```
第 1 批（安全加固，立即可做）
  ├── 1.5 添加 .gitignore
  ├── 1.2 auth.py 改用 secrets.compare_digest
  └── 1.4 SSL 默认值反转

第 2 批（核心可靠性）
  ├── 1.1 流式路径接入重试 + 熔断
  └── 1.3 CORS 白名单配置化

第 3 批（可维护性重构）
  ├── 2.1 拆分 codebuddy_router.py
  ├── 2.2 消除模块级副作用
  ├── 2.3 清理死代码
  └── 2.4 引入 Pydantic 请求模型

第 4 批（工程化完善）
  ├── 3.1 依赖分离
  ├── 3.2 Dockerfile
  ├── 3.3 日志统一
  ├── 3.4 Prometheus 指标
  └── 3.5 项目名称统一
```

### 每批次建议流程

1. 创建独立分支（如 `fix/security-hardening`、`refactor/router-split`）
2. 修改 + 补充/更新对应测试
3. `pytest tests/ -v` 全绿后合并
4. 更新本文档，勾选已完成项

---

*文档生成者：Claude Opus 4.6 · 审查范围：全量源码 + 配置 + 测试 + docker-compose*
