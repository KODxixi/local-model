"""补充功能集成测试：知识图谱 extract/find/related、查询改写、文档摘要。

kg 部分用规则提取（不调 LLM），在 tmp_path 建 LanceDB 图谱；
rewrite/summary 需要对话/视觉端点（默认 8080），用 skipif 条件跳过。
"""
from __future__ import annotations

import pytest
from conftest import requires_lm_studio
from knowledge_graph import KnowledgeGraph, extract_entities_regex

# ---------------------------------------------------------------------------
# 知识图谱（规则提取，不依赖 LLM）
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_kg_extract_find_related(tmp_path):
    """extract → find_docs_by_entity → find_related_entities 全链路。"""
    kg = KnowledgeGraph(db_path=tmp_path, kb_name="supp")

    # 规则提取实体
    content = '项目"智慧社区"采用《建筑设计规范》，"智慧社区"还提到了《消防规范》。'
    entities = extract_entities_regex(content)
    names = {e.name for e in entities}
    assert names, "规则提取无实体"

    added = kg.add_entities(entities, "/doc1.md", content)
    assert added >= 1

    # find：提到某实体的文档
    docs = kg.find_docs_by_entity(next(iter(names)), top_k=5)
    assert len(docs) >= 1
    assert docs[0]["path"] == "/doc1.md"

    # related：找共现实体
    related = kg.find_related_entities(next(iter(names)), top_k=5)
    # 同一文档内多个实体互为 related
    assert isinstance(related, list)

    # list / stats
    listing = kg.list_entities(top_k=10)
    assert len(listing) >= 1
    stats = kg.stats()
    assert stats["unique_entities"] >= 1


# ---------------------------------------------------------------------------
# 查询改写（需要 LM Studio 对话模型）
# ---------------------------------------------------------------------------

@requires_lm_studio
@pytest.mark.integration
def test_rewrite_query():
    """改写查询返回结构化结果。"""
    from rag_enhance import rewrite_query
    rw = rewrite_query("房子外面用什么材料比较好")
    assert rw.original
    assert rw.rewritten
    # 至少有改写后的主查询
    assert len(rw.rewritten) > 0


@requires_lm_studio
@pytest.mark.integration
def test_summary_document(tmp_path):
    """文档摘要返回 title/summary。"""
    from rag_enhance import summarize_document
    src = tmp_path / "note.md"
    src.write_text(
        "# 测试笔记\n\n这是一段关于住宅立面设计的长文本，涵盖材料选择、"
        "比例韵律和成本控制。面砖成本低，涂料色彩丰富，石材高端。",
        encoding="utf-8",
    )
    doc_text = src.read_text(encoding="utf-8")
    s = summarize_document(doc_text, title="测试笔记")
    assert s.title
    assert s.summary
