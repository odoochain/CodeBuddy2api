"""
可靠性工具模块 - 错误分类、指数退避重试、熔断器

提供以下能力：
- 错误分类：根据异常类型/状态码判断是否可重试
- 指数退避重试：异步上下文管理器与装饰器，支持抖动
- 熔断器：防止下游长时间不可用时持续打挂
- 限流：基于信号量的并发控制

设计目标：
- 无外部依赖（仅使用 stdlib + httpx）
- 线程/协程安全
- 与现有 SSEConnectionManager 兼容
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Awaitable, Callable, Iterable, Optional, Tuple, Type, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ErrorCategory(str, Enum):
    """错误分类：决定是否触发重试/熔断"""
    RETRYABLE_NETWORK = "retryable_network"     # 网络瞬断、连接超时
    RETRYABLE_SERVER = "retryable_server"       # 5xx、限流 429
    NON_RETRYABLE_CLIENT = "non_retryable_client"  # 4xx（除 408/429）
    UNKNOWN = "unknown"


# 可重试的 HTTP 状态码（参考 AWS / Google SRE 建议）
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404, 405, 409, 410, 415, 422})


def classify_exception(exc: BaseException) -> ErrorCategory:
    """
    将任意异常归类为 ErrorCategory。

    规则：
    - httpx.TimeoutException / NetworkError -> RETRYABLE_NETWORK
    - HTTPStatusError 且状态码在 RETRYABLE_STATUS_CODES -> RETRYABLE_SERVER
    - HTTPStatusError 且状态码在 NON_RETRYABLE_STATUS_CODES -> NON_RETRYABLE_CLIENT
    - asyncio.TimeoutError / ConnectionError -> RETRYABLE_NETWORK
    - 其他 -> UNKNOWN（默认不重试，避免放大问题）
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return ErrorCategory.RETRYABLE_NETWORK

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in RETRYABLE_STATUS_CODES:
            return ErrorCategory.RETRYABLE_SERVER
        if status in NON_RETRYABLE_STATUS_CODES:
            return ErrorCategory.NON_RETRYABLE_CLIENT
        return ErrorCategory.UNKNOWN

    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return ErrorCategory.RETRYABLE_NETWORK

    return ErrorCategory.UNKNOWN


def is_retryable(exc: BaseException) -> bool:
    """便捷判断：当前异常是否可重试"""
    cat = classify_exception(exc)
    return cat in (ErrorCategory.RETRYABLE_NETWORK, ErrorCategory.RETRYABLE_SERVER)


# ---------------- 退避策略 ----------------

@dataclass(frozen=True)
class RetryPolicy:
    """重试策略配置"""
    max_retries: int = 3                 # 最大重试次数（不含首次）
    initial_delay: float = 0.5           # 初始退避（秒）
    max_delay: float = 8.0               # 单次最大退避
    multiplier: float = 2.0              # 退避倍数
    jitter: float = 0.25                 # 抖动比例（±25%）
    retryable_exceptions: Tuple[Type[BaseException], ...] = (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        asyncio.TimeoutError,
        ConnectionError,
    )
    # 自定义重试判断（默认按 is_retryable）
    should_retry: Optional[Callable[[BaseException], bool]] = None

    def compute_delay(self, attempt: int) -> float:
        """
        计算第 attempt 次重试（从 0 开始）前的等待时间。
        含 full jitter：delay = random(0, base * multiplier^attempt)，但不超过 max_delay。
        """
        base = self.initial_delay * (self.multiplier ** attempt)
        base = min(base, self.max_delay)
        # 抖动：base ± jitter*base
        spread = base * self.jitter
        return max(0.0, random.uniform(base - spread, base + spread))


# ---------------- 重试执行器 ----------------

@dataclass
class RetryStats:
    """重试结果统计（用于测试/可观测）"""
    attempts: int = 0
    retried: int = 0
    succeeded: bool = False
    last_error: Optional[BaseException] = None


async def call_with_retry(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    stats: Optional[RetryStats] = None,
    **kwargs: Any,
) -> T:
    """
    以重试策略调用异步函数。

    - 第一次失败后按 RetryPolicy.compute_delay 退避
    - 只对 is_retryable 命中的异常重试
    - 达到 max_retries 后抛出最后一次异常
    - on_retry(attempt, delay, exc) 在每次重试前回调（可用于打日志/打 metric）
    """
    if policy is None:
        policy = RetryPolicy()
    should_retry = policy.should_retry or is_retryable

    if stats is None:
        stats = RetryStats()

    last_exc: Optional[BaseException] = None
    for attempt in range(policy.max_retries + 1):
        stats.attempts = attempt + 1
        try:
            result = await func(*args, **kwargs)
            stats.succeeded = True
            return result
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            stats.last_error = exc
            retryable = should_retry(exc) if callable(should_retry) else is_retryable(exc)
            if not retryable or attempt >= policy.max_retries:
                stats.succeeded = False
                raise

            delay = policy.compute_delay(attempt)
            stats.retried += 1
            logger.warning(
                f"[retry] {func.__name__} failed (attempt {attempt + 1}/{policy.max_retries + 1}): "
                f"{type(exc).__name__}: {exc}; sleeping {delay:.2f}s"
            )
            if on_retry is not None:
                try:
                    on_retry(attempt, delay, exc)
                except Exception:  # 回调失败不影响主流程
                    logger.exception("[retry] on_retry callback raised")
            await asyncio.sleep(delay)

    # 理论上不会到这里
    assert last_exc is not None
    raise last_exc


def retry(
    policy: Optional[RetryPolicy] = None,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """装饰器版本的重试（语法糖）"""
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await call_with_retry(func, *args, policy=policy, on_retry=on_retry, **kwargs)
        return wrapper
    return decorator


# ---------------- 熔断器 ----------------

class CircuitState(str, Enum):
    CLOSED = "closed"          # 正常
    OPEN = "open"              # 熔断
    HALF_OPEN = "half_open"    # 半开探测


@dataclass
class CircuitBreaker:
    """
    简单熔断器（CLOSED -> OPEN -> HALF_OPEN -> CLOSED/OPEN）。

    触发条件：连续 failure_threshold 次失败 -> 打开
    半开条件：open 状态持续 recovery_timeout 秒后 -> 允许一次探测
    探测成功 -> 关闭
    探测失败 -> 重新打开
    """
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    name: str = "default"

    state: CircuitState = CircuitState.CLOSED
    _failures: int = 0
    _opened_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _now(self) -> float:
        return time.monotonic()

    async def allow(self) -> bool:
        """是否允许执行：非熔断 或 已到半开探测时间"""
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.OPEN:
                if self._now() - self._opened_at >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info(f"[circuit:{self.name}] OPEN -> HALF_OPEN")
                    return True
                return False
            # HALF_OPEN：同时仅放行一个探测（简化：直接放行）
            return True

    async def record_success(self) -> None:
        async with self._lock:
            if self.state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                logger.info(f"[circuit:{self.name}] {self.state.value} -> CLOSED")
            self.state = CircuitState.CLOSED
            self._failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self._opened_at = self._now()
                logger.warning(f"[circuit:{self.name}] HALF_OPEN -> OPEN")
                return
            if self._failures >= self.failure_threshold and self.state == CircuitState.CLOSED:
                self.state = CircuitState.OPEN
                self._opened_at = self._now()
                logger.warning(
                    f"[circuit:{self.name}] CLOSED -> OPEN (failures={self._failures})"
                )

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self._failures,
            "opened_at": self._opened_at,
        }


class CircuitOpenError(RuntimeError):
    """熔断器打开时，调用被拒绝"""


@asynccontextmanager
async def guarded(breaker: CircuitBreaker):
    """
    用法：
        async with guarded(cb):
            await do_request()
    进入时检查 allow，结束时根据异常 record_success/record_failure。
    """
    if not await breaker.allow():
        raise CircuitOpenError(f"circuit '{breaker.name}' is OPEN")
    try:
        yield
    except BaseException:
        await breaker.record_failure()
        raise
    else:
        await breaker.record_success()


# ---------------- 并发限流 ----------------

class ConcurrencyLimiter:
    """
    基于 asyncio.Semaphore 的全局并发限流器。

    用途：保护下游 CodeBuddy API 不被瞬时突发打挂。
    """
    def __init__(self, max_concurrent: int = 64) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._in_flight = 0
        self._peak = 0
        self._total = 0
        self._lock = asyncio.Lock()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def peak(self) -> int:
        return self._peak

    @property
    def total(self) -> int:
        return self._total

    async def acquire(self) -> None:
        await self._sem.acquire()
        async with self._lock:
            self._in_flight += 1
            self._total += 1
            if self._in_flight > self._peak:
                self._peak = self._in_flight

    def release(self) -> None:
        self._sem.release()
        # _in_flight 减少不强制加锁（监控用统计，最终值近似即可）
        self._in_flight = max(0, self._in_flight - 1)

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            self.release()

    def snapshot(self) -> dict:
        return {
            "max_concurrent": self.max_concurrent,
            "in_flight": self._in_flight,
            "peak": self._peak,
            "total": self._total,
        }


# ---------------- 内存级响应缓存（用于幂等 GET） ----------------

@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    """
    简易线程/协程安全的 TTL 缓存，仅用于内部小规模幂等查询。
    不替代 Redis；用于 /v1/models 等元数据接口的减压。
    """
    def __init__(self, max_size: int = 256, default_ttl: float = 30.0) -> None:
        self.max_size = max(1, int(max_size))
        self.default_ttl = float(default_ttl)
        self._store: dict = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._store.pop(key, None)
                return None
            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        async with self._lock:
            if len(self._store) >= self.max_size:
                # 简化：pop 任意一项
                self._store.pop(next(iter(self._store)))
            self._store[key] = _CacheEntry(
                value=value,
                expires_at=time.monotonic() + (ttl if ttl is not None else self.default_ttl),
            )

    def clear(self) -> None:
        self._store.clear()


# ---------------- 模块级单例 ----------------

# 进程级默认限流器（可通过配置覆盖）
_default_limiter: Optional[ConcurrencyLimiter] = None
# 进程级默认熔断器（针对上游 CodeBuddy API）
_default_breaker: Optional[CircuitBreaker] = None
# 元数据缓存
_metadata_cache: Optional[TTLCache] = None


def get_default_limiter() -> ConcurrencyLimiter:
    global _default_limiter
    if _default_limiter is None:
        # 保守默认；可通过环境变量 CODEBUDDY_MAX_CONCURRENT 调整
        import os
        try:
            n = int(os.getenv("CODEBUDDY_MAX_CONCURRENT", "64"))
        except ValueError:
            n = 64
        _default_limiter = ConcurrencyLimiter(max_concurrent=n)
    return _default_limiter


def get_default_breaker() -> CircuitBreaker:
    global _default_breaker
    if _default_breaker is None:
        import os
        try:
            threshold = int(os.getenv("CODEBUDDY_BREAKER_THRESHOLD", "5"))
        except ValueError:
            threshold = 5
        try:
            recovery = float(os.getenv("CODEBUDDY_BREAKER_RECOVERY", "30"))
        except ValueError:
            recovery = 30.0
        _default_breaker = CircuitBreaker(
            failure_threshold=threshold,
            recovery_timeout=recovery,
            name="codebuddy_upstream",
        )
    return _default_breaker


def get_metadata_cache() -> TTLCache:
    global _metadata_cache
    if _metadata_cache is None:
        _metadata_cache = TTLCache(max_size=128, default_ttl=30.0)
    return _metadata_cache


def reset_for_tests() -> None:
    """测试钩子：重置单例"""
    global _default_limiter, _default_breaker, _metadata_cache
    _default_limiter = None
    _default_breaker = None
    _metadata_cache = None


__all__ = [
    "ErrorCategory",
    "RETRYABLE_STATUS_CODES",
    "NON_RETRYABLE_STATUS_CODES",
    "classify_exception",
    "is_retryable",
    "RetryPolicy",
    "RetryStats",
    "call_with_retry",
    "retry",
    "CircuitState",
    "CircuitBreaker",
    "CircuitOpenError",
    "guarded",
    "ConcurrencyLimiter",
    "TTLCache",
    "get_default_limiter",
    "get_default_breaker",
    "get_metadata_cache",
    "reset_for_tests",
]
