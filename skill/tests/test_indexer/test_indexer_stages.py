"""单元测试：RAGIndexer 各阶段方法（P2: index() 拆分）。

覆盖：
1. _discover_files 返回列表 + 增量 mtime 跳过
2. _index_single_file 单文件失败隔离（不影响其他文件）
3. 无文件时走 _skip_result（不触达 prune / optimize）
4. index() 主流程按正确顺序调用各阶段
5. _prune_orphans 同时清理 store 与 KG

全部用 mock / 临时目录隔离，不触发真实 embedding、LanceDB 写入或文件锁。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rag_indexer as ri  # noqa: E402
from rag_indexer import KBConfig, RAGIndexer  # noqa: E402

# ---------------------------------------------------------------------------
# 测试夹具：不构造真实 LanceDB，手工注入最小属性
# ---------------------------------------------------------------------------

def _make_indexer(root: Path, *, dimensions: int = 8) -> RAGIndexer:
    """构造一个未走 __init__ 的轻量 RAGIndexer（store / embed_fn 均为 mock）。"""
    kb = KBConfig(name="unit", root=str(root), patterns=["*.md"], dimensions=dimensions)
    idx = RAGIndexer.__new__(RAGIndexer)
    idx.kb = kb
    idx.db_path = Path(root) / ".db"
    idx.store = MagicMock()
    idx._embed_fn = MagicMock(return_value=[[0.0] * dimensions])
    idx.extract_entities = False
    idx._kg = None
    # 关闭真实 GPU 预热，避免测试期访问 9123
    idx.perf_config = {"warmup_on_index": False}
    idx._embed_batch_size = 64
    return idx


@pytest.fixture
def noop_lock(monkeypatch):
    """把并发文件锁替换为 no-op 上下文，避免测试读写真实锁文件。"""
    class _NoopLock:
        def __enter__(self) -> _NoopLock:
            return self

        def __exit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(ri, "_IndexLock", lambda *a, **k: _NoopLock())


# ---------------------------------------------------------------------------
# 1. _discover_files 返回列表（含增量 mtime 检查）
# ---------------------------------------------------------------------------

