"""pytest 配置 - 提供环境变量默认与共享 fixture。"""

import os
import sys
from pathlib import Path

# 确保 src 目录可被 import
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 在测试环境下注入最小可用的环境变量，避免 Optional import 失败
os.environ.setdefault("CODEBUDDY_PASSWORD", "test-password")
os.environ.setdefault("API_KEY", "test-api-key")
