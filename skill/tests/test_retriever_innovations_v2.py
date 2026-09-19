"""P4 创新突破 v2：智能混合权重 / 查询扩展 / 多轮上下文消歧 纯逻辑单测。

沿用 P3 测试风格：object.__new__ 绕过 __init__，把底层 recall / rerank /
LM Studio rewrite 全部 mock 掉，只验证三个新功能的编排与规则逻辑。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rag_indexer import KBConfig
from rag_retriever import RAGRetriever


def _make_retriever(recall_size: int = 24) -> RAGRetriever:
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
# P4-1: 智能混合权重
# ---------------------------------------------------------------------------

def test_smart_weights_short_query_keyword_heavy():
    """短查询（<5 字）→ keyword 权重 0.6，semantic 0.4。"""
    sw, kw = RAGRetriever._calculate_hybrid_weights("向量库")
    assert sw == pytest.approx(0.4)
    assert kw == pytest.approx(0.6)
    assert sw + kw == pytest.approx(1.0)


def test_smart_weights_long_query_semantic_heavy():
    """长自然语言查询（>15 字）→ semantic 权重 0.7，keyword 0.3。"""
    long_q = ("本文档完整记录了本地知识库系统从文档摄取分块向量化到召回排序"
              "的整套工程实践细节与性能调优经验")
    sw, kw = RAGRetriever._calculate_hybrid_weights(long_q)
    assert sw == pytest.approx(0.7)
    assert kw == pytest.approx(0.3)


def test_smart_weights_with_numbers_keyword_heavy():
    """含数字/百分比的事实型查询 → keyword 权重 0.8，semantic 0.2。"""
    sw, kw = RAGRetriever._calculate_hybrid_weights("2024年公司营收增长20%")
    assert sw == pytest.approx(0.2)
    assert kw == pytest.approx(0.8)


def test_smart_weights_default_balanced():
    """普通中等长度陈述查询 → 各 0.5。"""
    sw, kw = RAGRetriever._calculate_hybrid_weights("关于项目的普通描述")
    assert sw == pytest.approx(0.5)
    assert kw == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# P4-2: 查询扩展
# ---------------------------------------------------------------------------

def test_expand_query_returns_multiple(monkeypatch: pytest.MonkeyPatch):
    """mock rewrite_query 返回子查询 → 返回原查询 + 最多 N 个扩展。"""
    r = _make_retriever()

    class _RW:
        sub_queries = ["同义改写一", "同义改写二", "超出上限的第三句"]

    monkeypatch.setattr("rag_retriever.rewrite_query", lambda q: _RW())
    out = r._expand_query("原始查询", num_expansions=2)
    assert out == ["原始查询", "同义改写一", "同义改写二"]


def test_expand_query_fallback_on_error(monkeypatch: pytest.MonkeyPatch):
    """rewrite_query 抛异常（LM Studio 不可用）→ 降级为仅原查询。"""
    r = _make_retriever()

    def _boom(q: str):
        raise RuntimeError("LM Studio connection refused")

    monkeypatch.setattr("rag_retriever.rewrite_query", _boom)
    out = r._expand_query("原始查询", num_expansions=2)
    assert out == ["原始查询"]


# ---------------------------------------------------------------------------
# P4-3: 多轮上下文消歧
# ---------------------------------------------------------------------------

def test_disambiguate_with_pronoun():
    """含代词的查询被上下文消歧（带上最后一个名词短语）。"""
    out = RAGRetriever._disambiguate_query("它的原理是什么", "向量数据库")
    assert "向量数据库" in out
    assert "它" in out


def test_disambiguate_empty_context_returns_original():
    """无上下文时原样返回。"""
    assert RAGRetriever._disambiguate_query("任意查询", "") == "任意查询"


# ---------------------------------------------------------------------------
# 端到端集成
# ---------------------------------------------------------------------------

def test_hybrid_weights_in_metadata(monkeypatch: pytest.MonkeyPatch):
    """hybrid 检索返回的结果 metadata 含 hybrid_weights，且权重和=1。"""
    r = _make_retriever()

    monkeypatch.setattr(
        RAGRetriever, "_semantic_recall",
        lambda self, q, rs, pf, dtf: [_cand(1, score=0.9, chunk_id="a1", text="sem hit")],
    )
    monkeypatch.setattr(
        RAGRetriever, "_keyword_recall",
        lambda self, q, rs, pf: [_cand(2, score=0.8, chunk_id="k1", text="kw hit")],
    )
    monkeypatch.setattr(
        RAGRetriever, "_rerank",
        lambda self, q, cands, k: cands[:k],
    )

    results = r.retrieve("向量库", top_k=2, use_rerank=True)
    assert results
    w = results[0].metadata.get("hybrid_weights")
    assert w is not None
    assert abs(w["semantic"] + w["keyword"] - 1.0) < 1e-9
    # 短查询 "向量库" → keyword 0.6
    assert w["keyword"] == pytest.approx(0.6)


def test_retrieve_with_expand_and_context(monkeypatch: pytest.MonkeyPatch):
    """端到端：查询扩展 + 上下文消歧串联，metadata 打标正确。"""
    r = _make_retriever()

    class _RW:
        sub_queries = ["向量数据库的ANN检索", "embedding相似度搜索"]

    monkeypatch.setattr("rag_retriever.rewrite_query", lambda q: _RW())
    monkeypatch.setattr(
        RAGRetriever, "_semantic_recall",
        lambda self, q, rs, pf, dtf: [
            _cand(1, score=0.9, chunk_id="c1", text="向量数据库原理"),
        ],
    )
    monkeypatch.setattr(RAGRetriever, "_keyword_recall", lambda self, q, rs, pf: [])
    monkeypatch.setattr(
        RAGRetriever, "_rerank", lambda self, q, cands, k: cands[:k],
    )

    results = r.retrieve(
        "它是什么", top_k=3, use_rerank=False,
        expand_query=True, num_expansions=2, context="向量数据库",
    )
    assert results
    # 上下文消歧标记
    assert results[0].metadata.get("original_query") == "它是什么"
    assert "向量数据库" in results[0].metadata.get("disambiguated_query", "")
    # 查询扩展标记：原查询 + 2 子查询 = 3
    assert results[0].metadata.get("expanded") is True
    assert len(results[0].metadata.get("expanded_queries", [])) == 3
