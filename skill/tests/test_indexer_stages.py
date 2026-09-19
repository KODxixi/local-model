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

def test_discover_files_returns_list(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("# A\nhello", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\nworld", encoding="utf-8")
    # 不在 patterns 内，应被过滤
    (tmp_path / "note.txt").write_text("ignored", encoding="utf-8")

    idx = _make_indexer(tmp_path)

    # force=True：全量发现，无跳过
    to_index, discovered, skipped = idx._discover_files(force=True)
    assert discovered == 2
    assert skipped == 0
    assert isinstance(to_index, list)
    assert sorted(p.name for p in to_index) == ["a.md", "b.md"]

    # force=False：a.md 已索引且 mtime 一致 → 跳过；b.md 待索引
    a = tmp_path / "a.md"
    monkeypatch.setattr(
        idx, "_get_indexed_mtimes", lambda: {str(a): a.stat().st_mtime_ns}
    )
    to_index2, discovered2, skipped2 = idx._discover_files(force=False)
    assert discovered2 == 2
    assert skipped2 == 1
    assert [p.name for p in to_index2] == ["b.md"]


# ---------------------------------------------------------------------------
# 2. 单个文件失败隔离
# ---------------------------------------------------------------------------

def test_index_single_file_isolation(tmp_path, noop_lock):
    f1 = tmp_path / "bad.md"
    f2 = tmp_path / "good.md"
    f1.write_text("# bad", encoding="utf-8")
    f2.write_text("# good", encoding="utf-8")

    idx = _make_indexer(tmp_path)

    def fake_single(p: Path, *, extract_entities: bool):
        if p.name == "bad.md":
            raise RuntimeError("boom")
        return {"path": str(p), "chunks_embedded": 2, "chunks_upserted": 2, "entities_extracted": 0}

    idx._index_single_file = fake_single
    idx._discover_files = lambda *, force=False: ([f1, f2], 2, 0)
    idx._prune_orphans = MagicMock(return_value=0)
    idx.store.count.return_value = 2

    result = idx.index(force=True)

    # bad.md 失败被记录，good.md 正常索引，整体不中断
    assert result["files_failed"] == 1
    assert result["files_indexed"] == 1
    assert result["chunks_indexed"] == 2
    assert result["errors"][0]["path"].endswith("bad.md")


# ---------------------------------------------------------------------------
# 3. 无文件时走 _skip_result（不触达 prune / optimize）
# ---------------------------------------------------------------------------

def test_skip_result_when_no_files(tmp_path, noop_lock):
    idx = _make_indexer(tmp_path)
    # 3 个文件全部未变更 → files_to_index 为空
    idx._discover_files = lambda *, force=False: ([], 3, 3)
    idx.store.count.return_value = 5

    result = idx.index(force=False)

    assert result["files_indexed"] == 0
    assert result["files_discovered"] == 3
    assert result["files_skipped_unchanged"] == 3
    assert result["errors"] == []
    # 早返回：不应清理孤儿、不应 optimize
    idx.store.delete_orphans.assert_not_called()
    idx.store.optimize.assert_not_called()


# ---------------------------------------------------------------------------
# 4. index() 主流程按正确顺序调用各阶段
# ---------------------------------------------------------------------------

def test_index_main_flow_calls_stages_in_order(tmp_path, noop_lock):
    idx = _make_indexer(tmp_path)
    calls: list[str] = []

    p1 = tmp_path / "a.md"
    p2 = tmp_path / "b.md"
    p1.write_text("# a", encoding="utf-8")
    p2.write_text("# b", encoding="utf-8")

    idx._warmup_if_needed = lambda: calls.append("warmup")
    idx._discover_files = lambda *, force=False: ([p1, p2], 2, 0)

    def fake_single(p: Path, *, extract_entities: bool):
        calls.append(f"index:{p.name}")
        return {"path": str(p), "chunks_embedded": 1, "chunks_upserted": 1, "entities_extracted": 0}

    idx._index_single_file = fake_single
    idx._prune_orphans = lambda paths, *, do_entities=False: (calls.append("prune"), 0)[1]
    idx.store.optimize = lambda: calls.append("optimize")
    idx.store.count.return_value = 2

    idx.index(force=False, prune=True)

    assert calls == ["warmup", "index:a.md", "index:b.md", "prune", "optimize"]


# ---------------------------------------------------------------------------
# 5. _prune_orphans 同时清理 store 和 KG
# ---------------------------------------------------------------------------

def test_prune_orphans_calls_store_and_kg(tmp_path):
    idx = _make_indexer(tmp_path)
    idx.store.delete_orphans.return_value = 2
    kg = MagicMock()
    idx._kg = kg
    paths = {"/a.md", "/b.md"}

    # do_entities=True：store 与 KG 都清理
    n = idx._prune_orphans(paths, do_entities=True)
    assert n == 2
    idx.store.delete_orphans.assert_called_once_with(paths)
    kg.delete_orphans.assert_called_once_with(paths)

    # do_entities=False：只清 store，不动 KG
    idx.store.delete_orphans.reset_mock()
    kg.delete_orphans.reset_mock()
    idx._prune_orphans(paths, do_entities=False)
    idx.store.delete_orphans.assert_called_once_with(paths)
    kg.delete_orphans.assert_not_called()
