"""pytest 全局 fixture 与路径/服务检测工具。

设计原则（P0-2）：
- 本文件在 collect 阶段被 pytest 导入，**绝不能**在模块级触发任何
  真实 embedding / 索引构建 / 网络请求。外部服务探测只在
  pytest_collection_modifyitems 里、且确实收集到相关用例时才执行。
- 用 Path(__file__) 相对定位 scripts/，消除各测试文件里的硬编码绝对路径。
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径定位（相对项目根，不依赖硬编码绝对路径）
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
FIXTURES_DIR = TESTS_DIR / "fixtures"
TEST_KB_DIR = FIXTURES_DIR / "test-kb"
TEST_PDF_KB_DIR = FIXTURES_DIR / "test-pdf-kb"

# 把 scripts/ 加入 sys.path，各测试文件直接 `import rag_client` 等，
# 不再需要自己写 sys.path.insert。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# 外部服务检测（只在被调用时才发网络请求，模块级不调用）
# ---------------------------------------------------------------------------

def _http_get_ok(url: str, timeout: float = 2.0) -> bool:
    """轻量 GET 探测；任何异常都视为不可用。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "local-rag-tests"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        return False


def llama_swap_available() -> bool:
    """llama-swap (embedding/rerank, 9123) 是否在线。"""
    return _http_get_ok("http://127.0.0.1:9123/v1/models")


def lm_studio_available() -> bool:
    """对话/视觉端点 (VLM/chat, 8080) 是否在线。"""
    return _http_get_ok("http://127.0.0.1:8080/v1/models")


# ---------------------------------------------------------------------------
# pytest marker / skipif 辅助
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

# 这两个 decorator 只是打标记，**不加 skipif 条件**——条件在 collect 之后由
# pytest_collection_modifyitems 惰性判定，避免模块级（collect 阶段）联网。
requires_llama_swap = pytest.mark.requires_llama_swap
requires_lm_studio = pytest.mark.requires_lm_studio

_SERVICE_PROBES = {
    "requires_llama_swap": ("llama-swap 未运行 (http://127.0.0.1:9123)", llama_swap_available),
    "requires_lm_studio": ("对话/视觉端点 未运行 (http://127.0.0.1:8080)", lm_studio_available),
}


def pytest_configure(config: pytest.Config) -> None:
    for marker, (_, _) in _SERVICE_PROBES.items():
        config.addinivalue_line("markers", f"{marker}: 需要外部服务在线，离线时自动跳过")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """收集到需要外部服务的用例时才探测端点（collect 阶段不联网）。"""
    for marker, (reason, probe) in _SERVICE_PROBES.items():
        marked = [item for item in items if item.get_closest_marker(marker)]
        if not marked or probe():
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in marked:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def test_kb_dir() -> Path:
    """test-kb fixtures 目录（3 个建筑主题 md）。"""
    return TEST_KB_DIR


@pytest.fixture(scope="session")
def test_pdf_path() -> Path:
    """研究报告 PDF fixture。"""
    return TEST_PDF_KB_DIR / "research-report.pdf"
