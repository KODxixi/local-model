"""rag_retriever 纯逻辑单测：去重 + recall_size 截断。

不依赖真实向量库 / 模型：用 object.__new__ 绕过 __init__（避免打开 LanceDB），
把 store / embed / rerank 全部 mock 掉，只验证编排逻辑。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rag_indexer import KBConfig
from rag_retriever import RAGRetriever


def _make_retriever(recall_size: int = 10) -> RAGRetriever:
    """构造一个不打开任何磁盘/网络的 RAGRetriever。"""
    r = object.__new__(RAGRetriever)
    r.kb = KBConfig(name="test_kb", root="/tmp/x", dimensions=4)
    r.db_path = Path(".")
    r.store = MagicMock()
    r.rerank_model = "test-rerank"
    r.perf_config = {"rerank_recall_size": recall_size}
    return r


def _cand(i: int, score: float = 0.5, *, chunk_id: str | None = None,
          path: str = "/doc.md", text: str = "content") -> dict:
    return {
        "chunk_id": chunk_id or f"c{i}",
        "path": path,
        "text": text,
        "score": score,
        "doc_type": "text",
        "heading_path": [],
        "section_title": "",
    }


# ---------------------------------------------------------------------------
# _deduplicate
# ---------------------------------------------------------------------------

def test_deduplicate_same_chunk_id_keeps_first_higher_score():
    """同 chunk_id 的重复候选只保留第一条（语义召回通常在前=分数更高）。"""
    r = _make_retriever()
    cands = [
        _cand(1, score=0.9, chunk_id="dup"),  # 语义召回，高分
        _cand(1, score=0.1, chunk_id="dup"),  # 关键词召回重复，低分
        _cand(2, score=0.5, chunk_id="other"),
    ]
    out = r._deduplicate(cands)
    assert len(out) == 2
    assert out[0]["score"] == 0.9  # 保留高分的第一条
    assert out[0]["chunk_id"] == "dup"
    assert out[1]["chunk_id"] == "other"


def test_deduplicate_falls_back_to_path_text_when_no_chunk_id():
    """没有 chunk_id 时，用 path + text 前100字做 key 去重。"""
    r = _make_retriever()
    cands = [
        {"path": "/a.md", "text": "same text body", "score": 0.9},
        {"path": "/a.md", "text": "same text body", "score": 0.1},  # 同 path+text
        {"path": "/b.md", "text": "different body", "score": 0.5},
    ]
    out = r._deduplicate(cands)
    assert len(out) == 2
    assert out[0]["score"] == 0.9


# ---------------------------------------------------------------------------
# recall_size 截断（送 rerank 前按召回分排序截断）
# ---------------------------------------------------------------------------




def test_empty_query_raises():
    """空 query 抛 ValueError。"""
    r = _make_retriever()
    with pytest.raises(ValueError):
        r.retrieve("   ")



def test_dedup_key_same_prefix_different_text_not_merged():
    """同路径+相同前缀+不同后文的两个片段，不应被去重合并。

    复现 codex 复验失败用例：原实现截取前80字，导致两个不同片段被合成一条。
    """
    from rag_retriever import _dedup_key

    shared_prefix = "这是一段很长的共享前缀，用于测试去重键是否会错误地合并不同片段。" * 3
    r1 = {"chunk_id": "", "path": "/doc.md", "text": shared_prefix + "结尾A"}
    r2 = {"chunk_id": "", "path": "/doc.md", "text": shared_prefix + "结尾B"}

    assert _dedup_key(r1) != _dedup_key(r2), "不同后文的片段应有不同去重键"


def test_dedup_key_identical_chunks_same_key():
    """完全相同的片段（同路径+同内容）应有相同去重键，会被合并。"""
    from rag_retriever import _dedup_key

    r1 = {"chunk_id": "", "path": "/doc.md", "text": "完全相同的内容"}
    r2 = {"chunk_id": "", "path": "/doc.md", "text": "完全相同的内容"}

    assert _dedup_key(r1) == _dedup_key(r2)


def test_dedup_key_chunk_id_is_scoped_to_path():
    """chunk_id 在不同文件中可重复，去重键必须包含路径。"""
    from rag_retriever import _dedup_key

    r1 = {"chunk_id": "id-1", "path": "/a.md", "text": "内容A"}
    r2 = {"chunk_id": "id-1", "path": "/b.md", "text": "完全不同的内容B"}

    assert _dedup_key(r1) != _dedup_key(r2), "不同文件的同编号片段不能合并"


# ---------------------------------------------------------------------------
# smart_weights 一致性（codex 复验：多查询分支写死 True）
# ---------------------------------------------------------------------------


def test_hybrid_recall_dedup_same_prefix_different_text_keeps_both():
    """经过真实 _hybrid_recall：同路径+相同前缀+不同后文的两个片段，应保留 2 条。

    复现 codex 二次复验：_dedup_key 辅助函数正确，但 _hybrid_recall 调用处
    仍用前80字截取，导致不同片段被误合并。本测试直接调用 _hybrid_recall，
    防止再次出现"辅助函数正确、调用处漏接"。
    """
    r = _make_retriever()
    shared_prefix = "这是一段很长的共享前缀，用于测试 _hybrid_recall 去重是否会错误合并不同片段。" * 3

    sem_results = [
        {"chunk_id": "", "path": "/doc.md", "text": shared_prefix + "结尾A",
         "score": 0.9, "doc_type": "text", "heading_path": [], "section_title": "",
         "metadata": {}},
    ]
    kw_results = [
        {"chunk_id": "", "path": "/doc.md", "text": shared_prefix + "结尾B",
         "score": 0.0, "doc_type": "text", "heading_path": [], "section_title": "",
         "metadata": {}},
    ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda *a, **k: sem_results)
        mp.setattr(RAGRetriever, "_keyword_recall", lambda *a, **k: kw_results)
        merged = r._hybrid_recall("测试查询", recall_size=10, path_filter=None,
                                  doc_type_filter=None, smart_weights=False)

    assert len(merged) == 2, (
        f"同前缀不同后文的两个片段应保留 2 条，实际 {len(merged)} 条（去重误合并）"
    )
    texts = {m["text"] for m in merged}
    assert shared_prefix + "结尾A" in texts
    assert shared_prefix + "结尾B" in texts


def test_hybrid_recall_dedup_identical_chunks_merges():
    """经过真实 _hybrid_recall：完全相同的片段（同路径+同内容）应合并为 1 条。"""
    r = _make_retriever()
    same_text = "完全相同的内容用于测试合并"

    sem_results = [
        {"chunk_id": "", "path": "/doc.md", "text": same_text,
         "score": 0.9, "doc_type": "text", "heading_path": [], "section_title": "",
         "metadata": {}},
    ]
    kw_results = [
        {"chunk_id": "", "path": "/doc.md", "text": same_text,
         "score": 0.0, "doc_type": "text", "heading_path": [], "section_title": "",
         "metadata": {}},
    ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda *a, **k: sem_results)
        mp.setattr(RAGRetriever, "_keyword_recall", lambda *a, **k: kw_results)
        merged = r._hybrid_recall("测试查询", recall_size=10, path_filter=None,
                                  doc_type_filter=None, smart_weights=False)

    assert len(merged) == 1, f"完全相同的片段应合并为 1 条，实际 {len(merged)} 条"
    assert merged[0]["_source"] == "hybrid", "同时命中语义和关键词应标记为 hybrid"
