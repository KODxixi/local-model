"""E2E 集成测试：index → retrieve → freshness 完整工作流。

使用 fixtures/test-kb（3 个建筑主题 md），db 写入 tmp_path，
不污染 fixtures 目录。需要 llama-swap（9123）做真实 embedding。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="e2e 需要真实模型服务")
from conftest import requires_llama_swap
from rag_indexer import KBConfig, RAGIndexer
from rag_retriever import RAGRetriever


@pytest.fixture(scope="module")
def e2e_kb(test_kb_dir):
    return KBConfig(
        name="test_e2e",
        root=str(test_kb_dir),
        patterns=["*.md"],
        dimensions=4096,
        embed_model="text-embedding-qwen3-embedding-8b",
    )


@requires_llama_swap
@pytest.mark.integration
def test_e2e_index_retrieve_freshness(e2e_kb, tmp_path_factory):
    """完整链路：建索引 → 检索 → 新鲜度 → 增量更新。"""
    db_path = tmp_path_factory.mktemp("lancedb")

    # STEP 1: 建索引
    indexer = RAGIndexer(e2e_kb, db_path=db_path)
    result = indexer.index(force=True)
    assert result["files_discovered"] == 3
    assert result["files_indexed"] == 3
    assert result["chunks_indexed"] > 0
    assert result["total_chunks_in_store"] == result["chunks_indexed"]
    assert result["errors"] == []

    # STEP 2: 检索 —— 立面材料
    retriever = RAGRetriever(e2e_kb, db_path=db_path)
    results = retriever.retrieve("住宅立面材料", top_k=3, use_rerank=False)
    assert len(results) > 0, "立面材料查询无结果"
    assert any(r.score > 0.5 for r in results), "没有高相关结果"
    texts = " ".join(r.text for r in results)
    assert "立面" in texts or "材料" in texts

    # STEP 3: 检索 —— 户型动线
    results2 = retriever.retrieve("户型动线设计", top_k=3, use_rerank=False)
    assert len(results2) > 0
    assert any(r.score > 0.3 for r in results2)

    # STEP 4: 新鲜度检查（刚索引完应新鲜）
    fresh = indexer.check_freshness()
    assert fresh["is_fresh"] is True
    assert fresh["total_files"] == 3
    assert fresh["indexed_files"] == 3

    # STEP 5: 增量更新（无变更 → 全部跳过）
    result2 = indexer.index(force=False)
    assert result2["files_skipped_unchanged"] == 3
    assert result2["files_indexed"] == 0
