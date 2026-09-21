"""BM25 + retrieve 回归测试：真实临时 LanceDB，只 mock 模型调用。

覆盖：
- keyword_search BM25 返回值类型（metadata dict / heading_path list）
- keyword_search path_filter 字面包含
- hybrid retrieve（语义 + 关键词 RRF 融合）
- rerank 成功
- rerank 失败降级（保持 RRF 排序）
- FTS 索引懒加载创建
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skip(reason="bm25 回归测试需要真实 LanceDB")
from rag_retriever import KBConfig, RAGRetriever

# conftest 已把 scripts/ 加入 sys.path
from vector_store import LanceDBVectorStore, create_vector_store

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_lancedb(tmp_path: Path) -> Path:
    """临时 LanceDB 目录。"""
    return tmp_path / "lancedb"


@pytest.fixture
def sample_store(tmp_lancedb: Path) -> LanceDBVectorStore:
    """创建含测试数据的真实 LanceDBVectorStore。"""
    store = create_vector_store(
        db_path=str(tmp_lancedb),
        kb_name="test",
        dimensions=8,  # 小维度，mock embedding 用
    )
    # 插入测试数据
    records = [
        {
            "vector": [0.1] * 8,
            "chunk_id": "c1",
            "text": "住宅立面材料采用铝板和玻璃幕墙系统",
            "path": "/docs/住宅立面.md",
            "doc_type": "text",
            "heading_path": ["建筑", "立面"],
            "section_title": "立面材料",
            "start_line": 1,
            "end_line": 10,
            "mtime_ns": 1000,
            "model": "test-model",
            "metadata": {"author": "test", "tags": ["建筑"]},
        },
        {
            "vector": [0.2] * 8,
            "chunk_id": "c2",
            "text": "户型动线设计优化，实现公私分区",
            "path": "/docs/户型设计.md",
            "doc_type": "text",
            "heading_path": ["建筑", "户型"],
            "section_title": "动线",
            "start_line": 11,
            "end_line": 20,
            "mtime_ns": 1000,
            "model": "test-model",
            "metadata": {"author": "test"},
        },
        {
            "vector": [0.3] * 8,
            "chunk_id": "c3",
            "text": "向量索引构建使用 LanceDB，支持 ANN 搜索",
            "path": "/docs/向量索引.md",
            "doc_type": "text",
            "heading_path": ["技术", "索引"],
            "section_title": "LanceDB",
            "start_line": 21,
            "end_line": 30,
            "mtime_ns": 1000,
            "model": "test-model",
            "metadata": {"author": "test"},
        },
    ]
    store.upsert_chunks(records)
    return store


@pytest.fixture
def sample_kb() -> KBConfig:
    """测试用 KBConfig。"""
    return KBConfig(
        name="test",
        root="/tmp/test",
        embed_model="test-embed",
        dimensions=8,
    )


# ---------------------------------------------------------------------------
# 1. keyword_search BM25 返回值类型
# ---------------------------------------------------------------------------

class TestBM25ReturnType:
    """BM25 keyword_search 返回值必须与原接口一致。"""

    def test_metadata_is_dict(self, sample_store):
        """metadata 必须是 dict，不是 JSON 字符串。"""
        results = sample_store.keyword_search("住宅立面", top_k=5)
        assert len(results) > 0
        for r in results:
            assert isinstance(r["metadata"], dict), f"metadata 应为 dict，实际 {type(r['metadata'])}"

    def test_heading_path_is_list(self, sample_store):
        """heading_path 必须是 list，不是 JSON 字符串。"""
        results = sample_store.keyword_search("住宅立面", top_k=5)
        assert len(results) > 0
        for r in results:
            assert isinstance(r["heading_path"], list), f"heading_path 应为 list，实际 {type(r['heading_path'])}"

    def test_chunk_id_preserved(self, sample_store):
        """chunk_id 必须保留。"""
        results = sample_store.keyword_search("住宅立面", top_k=5)
        ids = {r["chunk_id"] for r in results}
        assert "c1" in ids

    def test_score_is_float_zero(self, sample_store):
        """BM25 结果 score 为 0.0（RRF 用排名）。"""
        results = sample_store.keyword_search("住宅立面", top_k=5)
        for r in results:
            assert r["score"] == 0.0


# ---------------------------------------------------------------------------
# 2. path_filter 字面包含
# ---------------------------------------------------------------------------

class TestPathFilter:
    """path_filter 必须是字面包含，不是 SQL LIKE 通配符。"""

    def test_path_filter_literal_match(self, sample_store):
        """path_filter 过滤出包含该子串的路径。"""
        results = sample_store.keyword_search("设计", top_k=10, path_filter="户型")
        paths = {r["path"] for r in results}
        assert all("户型" in p for p in paths)
        assert "/docs/户型设计.md" in paths

    def test_path_filter_no_match(self, sample_store):
        """path_filter 不匹配时返回空。"""
        results = sample_store.keyword_search("设计", top_k=10, path_filter="不存在的路径")
        assert results == []

    def test_path_filter_special_chars(self, sample_store):
        """path_filter 含 SQL 通配符时按字面处理（不做通配）。"""
        # % 在 SQL LIKE 中是通配符，但字面包含应不匹配
        results = sample_store.keyword_search("设计", top_k=10, path_filter="%")
        # 测试数据路径中不含 %，所以应为空
        assert results == []


# ---------------------------------------------------------------------------
# 2b. path_filter：目标候选位于全局前 2k 之外
# ---------------------------------------------------------------------------

class TestPathFilterBeyond2K:
    """path_filter 用 where(prefilter=True) 做服务端预过滤，不依赖取数上限。

    验证目标候选即使全局排名靠后，预过滤后仍能完整命中。
    """

    def test_target_beyond_2k_found_after_path_filter(self, tmp_lancedb):
        """30 个噪声文档 + 1 个目标文档，目标 BM25 分数最低（全局排名靠后）。"""
        store = create_vector_store(db_path=str(tmp_lancedb), kb_name="bigtest", dimensions=8)
        records = []
        # 30 个噪声文档："设计"出现 3 次，BM25 分数更高
        for i in range(30):
            records.append({
                "vector": [0.1] * 8,
                "chunk_id": f"noise{i:02d}",
                "text": f"设计方案{i} 设计细节 设计规范 其他内容填充",
                "path": f"/docs/noise{i:02d}.md",
                "doc_type": "text",
                "heading_path": [],
                "section_title": "",
                "start_line": 1,
                "end_line": 5,
                "mtime_ns": 1000,
                "model": "test",
                "metadata": {},
            })
        # 目标文档："设计"只出现 1 次，BM25 分数最低，全局排名靠后
        records.append({
            "vector": [0.1] * 8,
            "chunk_id": "target",
            "text": "设计 目标文档",
            "path": "/special/目标文档.md",
            "doc_type": "text",
            "heading_path": [],
            "section_title": "",
            "start_line": 1,
            "end_line": 5,
            "mtime_ns": 1000,
            "model": "test",
            "metadata": {},
        })
        store.upsert_chunks(records)

        # path_filter="special" 服务端预过滤，目标全局排名靠后仍能命中
        results = store.keyword_search("设计", top_k=5, path_filter="special")
        assert len(results) == 1, f"应找到 1 个目标，实际 {len(results)} 个"
        assert results[0]["chunk_id"] == "target"

    def test_1000_noise_outside_path_still_fully_matched(self, tmp_lancedb, monkeypatch):
        """超过 1000 条路径外噪声时，path_filter 预过滤仍能完整命中目标。

        验证 where(prefilter=True) 在服务端过滤，不依赖取数上限。
        即使目标候选全局排名 > 1000，预过滤后仍应返回所有目标。
        禁用 LIKE 降级，避免 BM25 回归被兜底掩盖。
        """
        store = create_vector_store(db_path=str(tmp_lancedb), kb_name="noisetest", dimensions=8)
        NOISE_COUNT = 1050  # 超过 1000，验证不依赖取数上限

        # 批量创建噪声文档（path 不含 "special"）
        noise_records = []
        for i in range(NOISE_COUNT):
            noise_records.append({
                "vector": [0.1] * 8,
                "chunk_id": f"noise{i:04d}",
                "text": f"设计方案{i} 设计细节 设计规范",
                "path": f"/docs/noise{i:04d}.md",
                "doc_type": "text",
                "heading_path": [],
                "section_title": "",
                "start_line": 1,
                "end_line": 5,
                "mtime_ns": 1000,
                "model": "test",
                "metadata": {},
            })

        # 2 条目标文档（path 含 "special"，BM25 分数最低）
        target_records = [
            {
                "vector": [0.1] * 8,
                "chunk_id": "target1",
                "text": "设计 目标一",
                "path": "/special/target1.md",
                "doc_type": "text",
                "heading_path": [],
                "section_title": "",
                "start_line": 1,
                "end_line": 5,
                "mtime_ns": 1000,
                "model": "test",
                "metadata": {},
            },
            {
                "vector": [0.1] * 8,
                "chunk_id": "target2",
                "text": "设计 目标二",
                "path": "/special/target2.md",
                "doc_type": "text",
                "heading_path": [],
                "section_title": "",
                "start_line": 1,
                "end_line": 5,
                "mtime_ns": 1000,
                "model": "test",
                "metadata": {},
            },
        ]

        store.upsert_chunks(noise_records + target_records)

        # 禁用 LIKE 降级：BM25 失败时直接抛异常，不被兜底掩盖
        def _like_disabled(*args, **kwargs):
            raise RuntimeError("LIKE 降级已禁用（大样本测试）")
        monkeypatch.setattr(store, "_keyword_search_like", _like_disabled)

        # path_filter="special" 应返回 2 条目标，即使全局排名 > 1000
        results = store.keyword_search("设计", top_k=10, path_filter="special")
        assert len(results) == 2, f"应找到 2 个目标，实际 {len(results)} 个"
        result_ids = {r["chunk_id"] for r in results}
        assert result_ids == {"target1", "target2"}, f"目标 ID 不匹配: {result_ids}"
        # 验证没有噪声文档混入
        assert all("special" in r["path"] for r in results)


# ---------------------------------------------------------------------------
# 3. FTS 索引懒加载
# ---------------------------------------------------------------------------

class TestFTSLazyInit:
    """FTS 索引首次 keyword_search 时自动创建。"""

    def test_fts_index_created_on_first_search(self, tmp_lancedb):
        """首次 keyword_search 后 FTS 索引应存在。"""
        store = create_vector_store(db_path=str(tmp_lancedb), kb_name="ftstest", dimensions=8)
        store.upsert_chunks([{
            "vector": [0.1] * 8,
            "chunk_id": "f1",
            "text": "测试文本用于 FTS 索引",
            "path": "/docs/test.md",
            "doc_type": "text",
            "heading_path": [],
            "section_title": "",
            "start_line": 1,
            "end_line": 5,
            "mtime_ns": 1000,
            "model": "test",
            "metadata": {},
        }])

        # 首次搜索应触发 FTS 索引创建
        assert not hasattr(store, "_fts_checked") or store._fts_checked is False
        store.keyword_search("测试", top_k=5)
        assert store._fts_checked is True

        # 验证索引存在
        indexes = store._table.list_indices()
        fts_indexes = [
            idx for idx in indexes
            if "FTS" in str(getattr(idx, "index_type", "")).upper()
        ]
        assert len(fts_indexes) > 0, "FTS 索引应已创建"


# ---------------------------------------------------------------------------
# 4. Hybrid retrieve（语义 + 关键词 RRF）
# ---------------------------------------------------------------------------

class TestHybridRetrieve:
    """RAGRetriever.retrieve 混合检索：语义 + 关键词 RRF 融合。"""

    @patch("rag_retriever.embed_texts")
    @patch("rag_retriever.rerank_texts")
    def test_hybrid_returns_merged_results(self, mock_rerank, mock_embed, sample_store, sample_kb, tmp_lancedb):
        """混合检索应返回语义和关键词的融合结果，rerank 成功时 ranked_by='rerank'。"""
        mock_embed.return_value = [[0.15] * 8]
        # rerank 返回结果的 index 必须在 candidates 范围内
        # sample_store 有 3 条记录，hybrid 召回最多 6 条（语义+关键词各 3），去重后约 3 条
        # 返回前 3 名，index 0/1/2
        mock_rerank.return_value = [
            {"index": 0, "score": 0.9, "text": "第一"},
            {"index": 1, "score": 0.8, "text": "第二"},
            {"index": 2, "score": 0.7, "text": "第三"},
        ]

        retriever = RAGRetriever(sample_kb, db_path=tmp_lancedb)
        results = retriever.retrieve("住宅立面", top_k=5, mode="hybrid", use_rerank=True)

        assert len(results) > 0
        # rerank 成功时所有结果 ranked_by 应为 "rerank"
        for r in results:
            assert r.ranked_by == "rerank", f"ranked_by 应为 rerank，实际 {r.ranked_by}"
        chunk_ids = {r.chunk_id for r in results}
        assert "c1" in chunk_ids

    @patch("rag_retriever.embed_texts")
    def test_hybrid_without_rerank(self, mock_embed, sample_store, sample_kb, tmp_lancedb):
        """不使用 rerank 时，按 RRF 分数排序；c1 同时命中语义+关键词应排第一。"""
        mock_embed.return_value = [[0.15] * 8]

        retriever = RAGRetriever(sample_kb, db_path=tmp_lancedb)
        results = retriever.retrieve("住宅立面", top_k=5, mode="hybrid", use_rerank=False)

        assert len(results) > 0
        # 不使用 rerank 时 ranked_by 应为 embedding 或 keyword（不是 rerank）
        for r in results:
            assert r.ranked_by in ("embedding", "keyword"), \
                f"不用 rerank 时 ranked_by 应为 embedding/keyword，实际 {r.ranked_by}"
        # c1 同时出现在语义和关键词结果中，RRF 融合分最高，应排第一
        assert results[0].chunk_id == "c1", \
            f"c1 应排第一（RRF 融合分最高），实际第一是 {results[0].chunk_id}"
        # 验证 hybrid 权重写入 metadata
        assert "hybrid_weights" in results[0].metadata, \
            "hybrid 结果应在 metadata 中写入 hybrid_weights"


# ---------------------------------------------------------------------------
# 5. rerank 失败降级
# ---------------------------------------------------------------------------

class TestRerankFailure:
    """rerank 失败时应降级到 RRF 排序，不应崩溃。"""

    @patch("rag_retriever.embed_texts")
    @patch("rag_retriever.rerank_texts")
    def test_rerank_failure_falls_back_to_rrf(self, mock_rerank, mock_embed, sample_store, sample_kb, tmp_lancedb):
        """rerank 抛异常时，结果应按 RRF 排序，metadata.rrf_score 应非零。"""
        mock_embed.return_value = [[0.15] * 8]
        mock_rerank.side_effect = RuntimeError("reranker unavailable")

        retriever = RAGRetriever(sample_kb, db_path=tmp_lancedb)
        results = retriever.retrieve("住宅立面", top_k=5, mode="hybrid", use_rerank=True)

        assert len(results) > 0
        # rerank 失败时 _sort_score 被复制到 metadata.rrf_score
        rrf_scores = [float(r.metadata.get("rrf_score", 0)) for r in results]
        # 验证非零（避免默认全零假通过）
        assert any(s > 0 for s in rrf_scores), "rrf_score 不应全为零"
        # 验证降序排列
        assert rrf_scores == sorted(rrf_scores, reverse=True), "rerank 失败后应按 RRF 降序"

    @patch("rag_retriever.embed_texts")
    @patch("rag_retriever.rerank_texts")
    def test_rerank_empty_results(self, mock_rerank, mock_embed, sample_store, sample_kb, tmp_lancedb):
        """rerank 返回空列表时不崩溃。"""
        mock_embed.return_value = [[0.15] * 8]
        mock_rerank.return_value = []

        retriever = RAGRetriever(sample_kb, db_path=tmp_lancedb)
        results = retriever.retrieve("住宅立面", top_k=5, mode="hybrid", use_rerank=True)

        assert results is not None


# ---------------------------------------------------------------------------
# 6. BM25 vs LIKE 降级
# ---------------------------------------------------------------------------

class TestBM25Fallback:
    """BM25 失败时应降级到 LIKE。"""

    def test_bm25_failure_falls_back_to_like(self, sample_store, monkeypatch):
        """FTS 查询失败时自动降级到 LIKE 包含匹配。"""
        # 让 FTS 查询抛异常
        original_search = sample_store._table.search

        def failing_search(*args, **kwargs):
            if kwargs.get("query_type") == "fts":
                raise RuntimeError("FTS index corrupted")
            return original_search(*args, **kwargs)

        monkeypatch.setattr(sample_store._table, "search", failing_search)

        # 应降级到 LIKE，仍能返回结果
        results = sample_store.keyword_search("住宅立面", top_k=5)
        assert len(results) > 0
        assert any(r["chunk_id"] == "c1" for r in results)
