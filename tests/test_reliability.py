"""
可靠性工具模块单元测试

覆盖：
- ErrorCategory / classify_exception / is_retryable
- RetryPolicy 退避计算
- call_with_retry 重试行为（成功 / 失败 / 不可重试 / 状态码可重试）
- CircuitBreaker 三态转换
- ConcurrencyLimiter 计数与 slot() 行为
- TTLCache 过期与命中
"""

import asyncio
import time

import httpx
import pytest

from src.reliability import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ConcurrencyLimiter,
    ErrorCategory,
    RetryPolicy,
    TTLCache,
    call_with_retry,
    classify_exception,
    get_default_breaker,
    get_default_limiter,
    get_metadata_cache,
    guarded,
    is_retryable,
    reset_for_tests,
)


# ---------- 错误分类 ----------

class TestClassifyException:
    def test_timeout_is_retryable_network(self):
        # httpx.TimeoutException 的具体子类
        exc = httpx.ConnectTimeout("timeout")
        assert classify_exception(exc) == ErrorCategory.RETRYABLE_NETWORK

    def test_network_error_is_retryable_network(self):
        exc = httpx.ConnectError("boom")
        assert classify_exception(exc) == ErrorCategory.RETRYABLE_NETWORK

    def test_retryable_status_codes(self):
        for status in (408, 425, 429, 500, 502, 503, 504):
            resp = httpx.Response(status_code=status)
            exc = httpx.HTTPStatusError("err", request=httpx.Request("GET", "http://x"), response=resp)
            assert classify_exception(exc) == ErrorCategory.RETRYABLE_SERVER, f"status {status}"

    def test_non_retryable_status_codes(self):
        for status in (400, 401, 403, 404, 422):
            resp = httpx.Response(status_code=status)
            exc = httpx.HTTPStatusError("err", request=httpx.Request("GET", "http://x"), response=resp)
            assert classify_exception(exc) == ErrorCategory.NON_RETRYABLE_CLIENT, f"status {status}"

    def test_unknown_status(self):
        resp = httpx.Response(status_code=418)  # I'm a teapot - 未知
        exc = httpx.HTTPStatusError("err", request=httpx.Request("GET", "http://x"), response=resp)
        assert classify_exception(exc) == ErrorCategory.UNKNOWN

    def test_unknown_exception(self):
        assert classify_exception(ValueError("x")) == ErrorCategory.UNKNOWN

    def test_is_retryable_shortcut(self):
        assert is_retryable(httpx.ConnectError("x"))
        assert is_retryable(httpx.ReadTimeout("x"))
        resp = httpx.Response(status_code=503)
        assert is_retryable(httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"), response=resp))
        # 不可重试
        resp = httpx.Response(status_code=400)
        assert not is_retryable(httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"), response=resp))
        # 未知
        assert not is_retryable(ValueError("x"))


# ---------- 退避策略 ----------

class TestRetryPolicy:
    def test_default_policy(self):
        p = RetryPolicy()
        assert p.max_retries == 3
        assert 0 < p.initial_delay <= p.max_delay
        assert p.multiplier >= 1.0

    def test_compute_delay_within_band(self):
        """compute_delay 应在 [base*(1-jitter), base*(1+jitter)] 范围内，且不超过 max_delay"""
        p = RetryPolicy(initial_delay=1.0, max_delay=10.0, multiplier=2.0, jitter=0.0)
        # jitter=0 时，delay 应严格等于 base，且第 0 次 = 1.0
        for attempt in range(3):
            expected = min(1.0 * (2.0 ** attempt), 10.0)
            assert p.compute_delay(attempt) == pytest.approx(expected, rel=1e-6)

    def test_compute_delay_caps_at_max(self):
        p = RetryPolicy(initial_delay=1.0, max_delay=5.0, multiplier=2.0, jitter=0.0)
        # 第 5 次本来是 16，会被截断到 5
        assert p.compute_delay(5) == 5.0


# ---------- call_with_retry ----------

class TestCallWithRetry:
    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            return "ok"

        result = await call_with_retry(op, policy=RetryPolicy(max_retries=2, initial_delay=0.01))
        assert result == "ok"
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_retryable_then_success(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            if calls["n"] < 2:
                raise httpx.ConnectError("transient")
            return "ok"

        result = await call_with_retry(op, policy=RetryPolicy(max_retries=3, initial_delay=0.01, max_delay=0.05))
        assert result == "ok"
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_exhausted_raises_last(self):
        calls = {"n": 0}

        async def op():
            calls["n"] += 1
            raise httpx.ConnectError(f"fail-{calls['n']}")

        with pytest.raises(httpx.ConnectError):
            await call_with_retry(op, policy=RetryPolicy(max_retries=2, initial_delay=0.01, max_delay=0.05))
        # 首次 + 2 次重试 = 3 次
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}
        resp = httpx.Response(status_code=400)

        async def op():
            calls["n"] += 1
            raise httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"), response=resp)

        with pytest.raises(httpx.HTTPStatusError):
            await call_with_retry(op, policy=RetryPolicy(max_retries=3, initial_delay=0.01))
        # 不可重试，仅调用 1 次
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_retry_on_retryable_status(self):
        calls = {"n": 0}
        responses = iter([
            httpx.Response(status_code=503),
            httpx.Response(status_code=200, json={"ok": True}),
        ])

        async def op():
            r = next(responses)
            calls["n"] += 1
            if r.status_code != 200:
                raise httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"), response=r)
            return r

        result = await call_with_retry(op, policy=RetryPolicy(max_retries=2, initial_delay=0.01))
        assert result.json() == {"ok": True}
        assert calls["n"] == 2


# ---------- CircuitBreaker ----------

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0, name="test")
        for _ in range(3):
            with pytest.raises(httpx.ConnectError):
                async with guarded(cb):
                    raise httpx.ConnectError("boom")
        assert cb.state == CircuitState.OPEN

        # 熔断打开后应直接抛 CircuitOpenError，不执行回调
        with pytest.raises(CircuitOpenError):
            async with guarded(cb):
                pytest.fail("should not run")

    @pytest.mark.asyncio
    async def test_success_keeps_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0, name="test")
        async with guarded(cb):
            pass  # 成功路径
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_recovery_to_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, name="test")
        with pytest.raises(httpx.ConnectError):
            async with guarded(cb):
                raise httpx.ConnectError("boom")
        assert cb.state == CircuitState.OPEN

        await asyncio.sleep(0.1)

        # 恢复超时后应进入 HALF_OPEN；一次成功调用后回到 CLOSED
        async with guarded(cb):
            pass

        assert cb.state == CircuitState.CLOSED


# ---------- ConcurrencyLimiter ----------

class TestConcurrencyLimiter:
    @pytest.mark.asyncio
    async def test_slot_limits_concurrency(self):
        lim = ConcurrencyLimiter(max_concurrent=2)
        peak = {"n": 0}
        cur = {"n": 0}

        async def task():
            async with lim.slot():
                cur["n"] += 1
                peak["n"] = max(peak["n"], cur["n"])
                await asyncio.sleep(0.05)
                cur["n"] -= 1

        await asyncio.gather(*(task() for _ in range(5)))
        assert peak["n"] <= 2
        snap = lim.snapshot()
        assert snap["in_flight"] == 0

    @pytest.mark.asyncio
    async def test_snapshot_under_load(self):
        lim = ConcurrencyLimiter(max_concurrent=3)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with lim.slot():
                started.set()
                await release.wait()

        t = asyncio.create_task(hold())
        await started.wait()
        snap = lim.snapshot()
        assert snap["in_flight"] == 1
        assert snap["max_concurrent"] == 3
        release.set()
        await t


# ---------- TTLCache ----------

class TestTTLCache:
    @pytest.mark.asyncio
    async def test_set_get(self):
        c = TTLCache(default_ttl=10)
        await c.set("k", "v")
        assert await c.get("k") == "v"

    @pytest.mark.asyncio
    async def test_expired_returns_none(self):
        c = TTLCache(default_ttl=0.05)
        await c.set("k", "v")
        assert await c.get("k") == "v"
        await asyncio.sleep(0.1)
        assert await c.get("k") is None

    @pytest.mark.asyncio
    async def test_clear(self):
        c = TTLCache(default_ttl=10)
        await c.set("k", "v")
        c.clear()
        assert await c.get("k") is None


# ---------- 默认实例 ----------

class TestDefaultInstances:
    def test_default_limiter_singleton(self):
        reset_for_tests()
        a = get_default_limiter()
        b = get_default_limiter()
        assert a is b

    def test_default_breaker_singleton(self):
        reset_for_tests()
        a = get_default_breaker()
        b = get_default_breaker()
        assert a is b

    def test_metadata_cache_singleton(self):
        reset_for_tests()
        a = get_metadata_cache()
        b = get_metadata_cache()
        assert a is b
