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

def test_recall_size_truncation_before_rerank():
    """hybrid 召回 30 条 > recall_size=10 → rerank 只收到 10 条。"""
    r = _make_retriever(recall_size=10)
    semantic = [_cand(i, score=0.9 - i * 0.01) for i in range(30)]

    captured: dict = {}

    def fake_rerank(self, query, cands, top_k):
        captured["n"] = len(cands)
        # 截断后应按召回分降序，前 10 条
        return [dict(c) for c in cands[:top_k]]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda self, q, rs, pf, dtf: semantic)
        mp.setattr(RAGRetriever, "_keyword_recall", lambda self, q, rs, pf: [])
        mp.setattr(RAGRetriever, "_rerank", fake_rerank)
        results = r.retrieve("query", top_k=5, use_rerank=True, mode="hybrid")

    assert captured["n"] == 10  # 截断到 recall_size
    assert len(results) == 5  # 最终 top_k


def test_no_truncation_when_candidates_within_recall_size():
    """候选数 <= recall_size 时不截断，原样送 rerank。"""
    r = _make_retriever(recall_size=24)
    semantic = [_cand(i, score=0.9 - i * 0.01) for i in range(8)]
    captured: dict = {}

    def fake_rerank(self, query, cands, top_k):
        captured["n"] = len(cands)
        return [dict(c) for c in cands[:top_k]]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda self, q, rs, pf, dtf: semantic)
        mp.setattr(RAGRetriever, "_keyword_recall", lambda self, q, rs, pf: [])
        mp.setattr(RAGRetriever, "_rerank", fake_rerank)
        r.retrieve("query", top_k=5, use_rerank=True, mode="hybrid")

    assert captured["n"] == 8


def test_keyword_mode_skips_rerank_and_truncation():
    """keyword 模式不 rerank、不截断，直接按召回分取 top_k。"""
    r = _make_retriever(recall_size=5)
    kw = [_cand(i, score=float(i)) for i in range(10)]
    kw[0]["_source"] = "keyword"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda *a, **k: [])
        mp.setattr(RAGRetriever, "_keyword_recall", lambda self, q, rs, pf: kw)
        # 若 rerank 被调用则测试失败
        def must_not_rerank(*a, **k):
            raise AssertionError("keyword mode must not call rerank")
        mp.setattr(RAGRetriever, "_rerank", must_not_rerank)
        results = r.retrieve("query", top_k=3, use_rerank=True, mode="keyword")

    assert len(results) == 3
    # 按 score 降序
    assert results[0].score >= results[-1].score
    assert all(rr.ranked_by == "keyword" for rr in results)


def test_empty_query_raises():
    """空 query 抛 ValueError。"""
    r = _make_retriever()
    with pytest.raises(ValueError):
        r.retrieve("   ")


def test_no_candidates_returns_empty_list():
    """召回为空直接返回 []，不调用 rerank。"""
    r = _make_retriever()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", lambda *a, **k: [])
        mp.setattr(RAGRetriever, "_keyword_recall", lambda *a, **k: [])
        results = r.retrieve("query", top_k=5, use_rerank=True)
    assert results == []


# ---------------------------------------------------------------------------
# _dedup_key 稳定去重（codex 复验：原前80字截取会误合并）
# ---------------------------------------------------------------------------

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


def test_dedup_key_chunk_id_takes_priority():
    """有 chunk_id 时优先用 chunk_id，不依赖 path+text。"""
    from rag_retriever import _dedup_key

    r1 = {"chunk_id": "id-1", "path": "/a.md", "text": "内容A"}
    r2 = {"chunk_id": "id-1", "path": "/b.md", "text": "完全不同的内容B"}

    assert _dedup_key(r1) == _dedup_key(r2), "同 chunk_id 应视为同一片段"


# ---------------------------------------------------------------------------
# smart_weights 一致性（codex 复验：多查询分支写死 True）
# ---------------------------------------------------------------------------

def test_smart_weights_false_multi_query_uses_equal_weights():
    """smart_weights=False 时，多查询改写分支也应保持等权(0.5/0.5)，不变成 0.2/0.8。

    复现 codex 复验：原 _multi_query_retrieve 写死 smart_weights=True，
    导致普通查询等权、开启改写后变成智能权重，第一名随之改变。

    P0 修复（codex 二次复验）：原测试 mock rag_enhance.rewrite_query，
    但 rag_retriever 已 from rag_enhance import rewrite_query，mock 不生效，
    多查询分支调用次数为 0 测试仍通过（假通过）。改为 mock rag_retriever.rewrite_query，
    并断言 _hybrid_recall 被调用 2 次（原查询+子查询），证明多查询分支确实执行。
    """
    import rag_retriever

    r = _make_retriever()
    captured_weights: list[tuple[float, float]] = []

    def fake_hybrid_recall(self, query, rs, pf, dtf, smart_weights=True):
        sw, kw = (0.5, 0.5)
        if smart_weights:
            sw, kw = (0.2, 0.8)  # 模拟智能权重的极端值
        captured_weights.append((sw, kw))
        return [_cand(1, score=0.9, text=f"result-{query}")]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_hybrid_recall", fake_hybrid_recall)
        mp.setattr(RAGRetriever, "_rerank", lambda self, q, cands, tk: cands[:tk])
        # P0 修复：mock rag_retriever 模块中已导入的 rewrite_query，不是 rag_enhance
        mp.setattr(
            rag_retriever, "rewrite_query",
            lambda q, context=None: type("R", (), {
                "original": q, "rewritten": q, "sub_queries": ["子查询1"],
                "keywords": [], "reasoning": "",
            })(),
        )
        r.retrieve("测试查询", top_k=3, use_rerank=False, mode="hybrid",
                   use_query_rewrite=True, smart_weights=False)

    # 断言多查询分支确实执行：原查询 + 1 个子查询 = 2 次 _hybrid_recall 调用
    assert len(captured_weights) == 2, (
        f"多查询分支应执行 2 次 _hybrid_recall（原查询+子查询），实际 {len(captured_weights)} 次"
    )
    for sw, kw in captured_weights:
        assert (sw, kw) == (0.5, 0.5), f"smart_weights=False 时应等权，实际 {sw}/{kw}"


# ---------------------------------------------------------------------------
# _hybrid_recall 真实去重（codex 二次复验：辅助函数正确但调用处漏接）
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
