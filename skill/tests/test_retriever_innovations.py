"""P3 创新突破：MMR / Parent-Child / Query Routing 纯逻辑单测。

不依赖真实向量库 / 模型：用 object.__new__ 绕过 __init__，把底层
recall / rerank 全部 mock 掉，只验证三个新功能的编排逻辑。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rag_indexer import KBConfig
from rag_retriever import RAGRetriever, RetrievalResult


def _make_retriever(recall_size: int = 24) -> RAGRetriever:
    """构造一个不打开任何磁盘/网络的 RAGRetriever。"""
    r = object.__new__(RAGRetriever)
    r.kb = KBConfig(name="test_kb", root="/tmp/x", dimensions=4)
    r.db_path = Path(".")
    r.store = MagicMock()
    r.rerank_model = "test-rerank"
    r.perf_config = {"rerank_recall_size": recall_size}
    return r


def _res(text: str, score: float, *, path: str = "/doc.md",
         chunk_id: str = "c", metadata: dict | None = None) -> RetrievalResult:
    return RetrievalResult(
        text=text, path=path, score=score, chunk_id=chunk_id,
        metadata=metadata or {},
    )


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
# P3-1: MMR 多样性重排
# ---------------------------------------------------------------------------

def test_mmr_reorder_reduces_similarity():
    """高分前 3 条高度相似时，MMR 应引入至少一个多样化 chunk。"""
    r = _make_retriever()
    texts = [
        ("向量数据库的检索原理是 ANN 近似最近邻搜索，通过 embedding 向量召回相关文档", 0.95),
        ("向量数据库的检索原理是 ANN 近似最近邻搜索，embedding 向量召回相关文档", 0.90),
        ("向量数据库检索原理 近似最近邻 ANN 搜索 embedding 召回", 0.85),
        ("如何种植西红柿 浇水施肥光照 阳台园艺入门", 0.70),
        ("公司年度财报 营收增长 2024年 利润同比上升", 0.60),
    ]
    results = [_res(t, s, chunk_id=f"c{i}") for i, (t, s) in enumerate(texts)]
    mmr = r._mmr_reorder(results, None, lambda_mult=0.5, top_k=3)
    assert len(mmr) == 3
    mmr_ids = {x.chunk_id for x in mmr}
    # 朴素 top3 = {c0,c1,c2}（扎堆）；MMR 不应全选相似 chunk
    assert mmr_ids != {"c0", "c1", "c2"}


def test_mmr_lambda_zero_pure_diversity():
    """lambda=0 纯多样性：第二个必选与第一个最不同的 chunk。"""
    r = _make_retriever()
    texts = [
        ("向量数据库 ANN 近似最近邻 embedding 向量检索原理", 0.9),   # c0
        ("向量数据库 ANN 近似最近邻 embedding 检索", 0.8),           # c1 与 c0 高度相似
        ("西红柿种植 阳台 浇水 园艺 光照 施肥", 0.7),               # c2 完全不同
    ]
    results = [_res(t, s, chunk_id=f"c{i}") for i, (t, s) in enumerate(texts)]
    out = r._mmr_reorder(results, None, lambda_mult=0.0, top_k=2)
    assert out[0].chunk_id == "c0"  # 并列时取第一个
    assert out[1].chunk_id == "c2"  # 最不同的，而非相似的 c1


def test_mmr_lambda_one_pure_relevance():
    """lambda=1 纯相关性：MMR = relevance，保持原（按分降序）顺序。"""
    r = _make_retriever()
    texts = [
        ("alpha 向量数据库 检索原理 ANN", 0.9),
        ("beta 西红柿种植 浇水施肥", 0.8),
        ("gamma 财报 营收 2024", 0.7),
    ]
    results = [_res(t, s, chunk_id=f"c{i}") for i, (t, s) in enumerate(texts)]
    out = r._mmr_reorder(results, None, lambda_mult=1.0, top_k=3)
    assert [x.chunk_id for x in out] == ["c0", "c1", "c2"]


# ---------------------------------------------------------------------------
# P3-2: Parent-Child 上下文扩展
# ---------------------------------------------------------------------------

def test_parent_child_expands_context(tmp_path: Path):
    """命中 chunk 应带上前后文，并打扩展标记。"""
    r = _make_retriever()
    body = "这是前文内容" * 50 + "命中的核心 chunk 文本" + "这是后文内容" * 50
    f = tmp_path / "doc.md"
    f.write_text(body, encoding="utf-8")

    res = _res("命中的核心 chunk 文本", 0.9, path=str(f))
    out = r._expand_to_parent([res], expand_chars=20)

    assert len(out) == 1
    text = out[0].text
    assert "[上下文前]" in text
    assert "[命中]" in text
    assert "[上下文后]" in text
    assert out[0].metadata.get("expanded") is True
    assert out[0].metadata.get("expand_chars") == 20


def test_parent_child_skip_deleted_file():
    """原始文件不存在时跳过扩展，保留原 chunk，不抛异常。"""
    r = _make_retriever()
    ghost = Path("C:/nonexistent/definitely_gone_xyz_42.md")
    res = _res("some chunk text", 0.9, path=str(ghost))
    out = r._expand_to_parent([res], expand_chars=100)
    assert len(out) == 1
    assert out[0].text == "some chunk text"
    assert out[0].metadata.get("expanded") is None


# ---------------------------------------------------------------------------
# P3-3: 查询路由
# ---------------------------------------------------------------------------

def test_route_query_keyword():
    """含数字/日期/百分比的事实型查询 → keyword。"""
    r = _make_retriever()
    assert r._route_query("2024年公司营收增长20%") == "keyword"


def test_route_query_semantic():
    """含"什么是"的开放型查询 → semantic。"""
    r = _make_retriever()
    assert r._route_query("什么是向量数据库") == "semantic"


def test_route_query_hybrid_default():
    """普通陈述型查询（无事实词、无疑问词）→ hybrid。"""
    r = _make_retriever()
    assert r._route_query("关于项目的一些普通描述") == "hybrid"


# ---------------------------------------------------------------------------
# 集成：auto_route 打标 + retrieve 端到端
# ---------------------------------------------------------------------------

def test_auto_route_marks_routed_mode():
    """auto_route=True 时返回结果 metadata 带 routed_mode。"""
    r = _make_retriever()
    kw = [_cand(1, score=0.9, chunk_id="k1", path="/kw.md", text="2024 营收财报")]
    captured: dict = {}

    def fake_semantic(self, q, rs, pf, dtf):
        captured["semantic_called"] = True
        return []

    def fake_keyword(self, q, rs, pf):
        captured["keyword_called"] = True
        return kw

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall", fake_semantic)
        mp.setattr(RAGRetriever, "_keyword_recall", fake_keyword)
        results = r.retrieve("2024年营收增长", top_k=3, use_rerank=False,
                             auto_route=True)

    # 事实型查询路由到 keyword → 不调语义召回
    assert captured.get("keyword_called") is True
    assert "semantic_called" not in captured
    assert results and results[0].metadata.get("routed_mode") == "keyword"


def test_retrieve_with_mmr_and_parent_child(tmp_path: Path):
    """端到端：MMR 重排 + parent-child 扩展串联工作。"""
    r = _make_retriever(recall_size=24)
    body = ("前言内容 " * 30) + "核心命中段落 向量数据库 ANN 检索原理" + (" 尾部内容" * 30)
    f = tmp_path / "doc.md"
    f.write_text(body, encoding="utf-8")

    semantic = [
        {"chunk_id": "a1", "path": str(f),
         "text": "核心命中段落 向量数据库 ANN 检索原理", "score": 0.9,
         "doc_type": "text", "heading_path": [], "section_title": ""},
        {"chunk_id": "a2", "path": str(f),
         "text": "核心命中段落 向量数据库 ANN 检索原理", "score": 0.85,
         "doc_type": "text", "heading_path": [], "section_title": ""},
        {"chunk_id": "b1", "path": str(f),
         "text": "无关的 园艺 种植 浇水 西红柿", "score": 0.7,
         "doc_type": "text", "heading_path": [], "section_title": ""},
    ]

    def fake_rerank(self, q, cands, top_k):
        return [dict(c) for c in cands[:top_k]]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RAGRetriever, "_semantic_recall",
                   lambda self, q, rs, pf, dtf: semantic)
        mp.setattr(RAGRetriever, "_keyword_recall",
                   lambda self, q, rs, pf: [])
        mp.setattr(RAGRetriever, "_rerank", fake_rerank)
        results = r.retrieve(
            "向量数据库", top_k=2, use_rerank=True,
            use_mmr=True, parent_child=True, parent_expand_chars=15,
        )

    assert len(results) == 2
    # 第一条必是最高分 a1（MMR 起点），且 parent-child 已扩展
    assert results[0].chunk_id == "a1"
    assert results[0].metadata.get("expanded") is True
    assert "[命中]" in results[0].text
