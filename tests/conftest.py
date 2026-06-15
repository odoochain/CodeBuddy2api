"""
pytest 共享 fixtures

- 提供 mock 的 IMA 凭证
- 注入临时环境变量，避免污染真实配置
"""
import os
import pytest

TEST_CLIENT_ID = "test_client_id_abc"
TEST_API_KEY = "test_api_key_xyz"


@pytest.fixture(autouse=True)
def ima_test_env(monkeypatch):
    """为所有测试注入 IMA 凭证环境变量"""
    monkeypatch.setenv("IMA_OPENAPI_CLIENTID", TEST_CLIENT_ID)
    monkeypatch.setenv("IMA_OPENAPI_APIKEY", TEST_API_KEY)
    # 重置默认 client 单例，确保读取新的环境变量
    import src.ima_client as ima_client_module
    ima_client_module.reset_ima_client()
    yield
    ima_client_module.reset_ima_client()
