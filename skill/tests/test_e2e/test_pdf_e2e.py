"""PDF E2E 集成测试：PDF 解析 → 索引 → 检索。

使用 fixtures/test-pdf-kb/research-report.pdf，db 写入 tmp_path。
需要 llama-swap（9123）做文本/图片 embedding。
"""
from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(reason="e2e 需要真实模型服务")
from conftest import requires_llama_swap
from rag_indexer import KBConfig, RAGIndexer
from rag_retriever import RAGRetriever


@pytest.fixture(scope="module")
def pdf_kb(test_pdf_path):
    return KBConfig(
        name="test_pdf",
        root=str(test_pdf_path.parent),
        patterns=["*.pdf"],
        dimensions=4096,
        embed_model="text-embedding-qwen3-embedding-8b",
    )


@requires_llama_swap
@pytest.mark.integration
def test_pdf_index_and_retrieve(pdf_kb, tmp_path_factory):
    """研究报告 PDF：建索引后能检索到业务关键词。"""
    db_path = tmp_path_factory.mktemp("pdf_lancedb")

    indexer = RAGIndexer(pdf_kb, db_path=db_path)
    result = indexer.index(force=True)
    assert result["files_indexed"] >= 1
    assert result["chunks_indexed"] > 0
    assert result["errors"] == [], f"PDF 索引错误: {result['errors']}"

    retriever = RAGRetriever(pdf_kb, db_path=db_path)

    # 两个业务关键词都应命中
    for query in ("分阶段交付", "确认收入"):
        results = retriever.retrieve(query, top_k=3, use_rerank=False)
        assert len(results) > 0, f"{query} 检索无结果"
        assert any(r.score > 0.3 for r in results), f"{query} 无高相关结果"
