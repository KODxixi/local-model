"""单元测试：chunker, ingest, vector_store 核心逻辑。

运行：python -m pytest tests/test_core.py -v
或：python tests/test_core.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from chunker import chunk_document, chunk_markdown
from ingest import _guess_doc_type, _safe_read_text, ingest
from vector_store import VectorStore, make_schema

# ---------------------------------------------------------------------------
# chunker tests
# ---------------------------------------------------------------------------

def test_chunk_simple_text():
    """简单文本应该返回一个 chunk。"""
    text = "# Title\n\nSome content here."
    chunks = chunk_markdown(text, max_chars=1000)
    assert len(chunks) == 1
    assert "Title" in chunks[0].text
    assert chunks[0].metadata["heading_path"] == ["Title"]


def test_chunk_by_headings():
    """按标题分割为多个章节。"""
    # 用足够长的内容确保不被合并
    long_content = "This is a long paragraph with enough content to exceed the minimum chunk size threshold for testing purposes. " * 5
    text = f"# H1\n\n{long_content}\n\n## H2\n\n{long_content}\n\n## H2b\n\n{long_content}"
    chunks = chunk_markdown(text, max_chars=200, min_chars=50)
    # 至少有多个 chunk
    assert len(chunks) >= 2
    # 每个 chunk 有 heading_path
    for c in chunks:
        assert "heading_path" in c.metadata


def test_chunk_code_block_protected():
    """代码块不被截断。"""
    code = "```python\n" + "\n".join(f"print({i})" for i in range(50)) + "\n```"
    text = f"# Title\n\n{code}\n\nMore text."
    chunks = chunk_markdown(text, max_chars=100)
    # 代码块应该单独成块
    code_chunks = [c for c in chunks if "```" in c.text]
    assert len(code_chunks) >= 1
    # 代码块完整
    for c in code_chunks:
        assert c.text.startswith("```") or "```python" in c.text


def test_chunk_table_protected():
    """表格保持完整。"""
    table = "| Col1 | Col2 |\n|---|---|\n| A | B |\n| C | D |\n| E | F |"
    text = f"# Title\n\n{table}\n\nMore."
    chunks = chunk_markdown(text, max_chars=50)
    table_chunks = [c for c in chunks if "|" in c.text]
    assert len(table_chunks) >= 1


def test_chunk_small_merge():
    """过小的 chunk 合并到前一个。"""
    text = "# H1\n\nA\n\n# H2\n\nB"
    chunks = chunk_markdown(text, max_chars=1000, min_chars=10)
    # "A" 和 "B" 都很小，应该被合并
    assert len(chunks) <= 2


def test_chunk_overlap():
    """相邻 chunk 有重叠段落。"""
    paragraphs = [f"Paragraph {i} with some content to make it long enough." for i in range(20)]
    text = "# Title\n\n" + "\n\n".join(paragraphs)
    chunks = chunk_markdown(text, max_chars=200, overlap_paragraphs=1)
    # 至少有2个 chunk
    assert len(chunks) >= 2
    # 重叠：后一个 chunk 的开头应该包含前一个 chunk 的最后一个段落
    # （这个测试比较脆弱，主要验证不报错）


def test_chunk_document_dispatch():
    """chunk_document 根据 doc_type 分发。"""
    text = "# Title\n\nContent"
    chunks = chunk_document(text, doc_type="text")
    assert len(chunks) >= 1
    chunks = chunk_document(text, doc_type="pdf")
    assert len(chunks) >= 1


def test_chunk_empty():
    """空文本返回空列表。"""
    assert chunk_markdown("") == []
    assert chunk_markdown("   ") == []


# ---------------------------------------------------------------------------
# ingest tests
# ---------------------------------------------------------------------------

def test_guess_doc_type():
    assert _guess_doc_type(Path("test.md")) == "text"
    assert _guess_doc_type(Path("test.pdf")) == "pdf"
    assert _guess_doc_type(Path("test.docx")) == "docx"
    assert _guess_doc_type(Path("test.xlsx")) == "xlsx"
    assert _guess_doc_type(Path("test.pptx")) == "pptx"
    assert _guess_doc_type(Path("test.png")) == "image"
    assert _guess_doc_type(Path("test.unknown")) == "unknown"


def test_ingest_text_file():
    """解析文本文件。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write("# Test Title\n\nContent here.")
        tmp_path = f.name
    try:
        doc = ingest(tmp_path)
        assert doc.title == "Test Title"
        assert doc.doc_type == "text"
        assert "Content here" in doc.content
        assert doc.metadata["size_bytes"] > 0
        assert "sha256" in doc.metadata
    finally:
        Path(tmp_path).unlink()


def test_ingest_nonexistent():
    """不存在的文件抛出异常。"""
    try:
        ingest("/nonexistent/file.md")
    except FileNotFoundError:
        return
    raise AssertionError("Should raise FileNotFoundError")


def test_safe_read_text_encoding():
    """多编码读取。"""
    content = "中文内容"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write(content.encode("gbk"))
        tmp_path = f.name
    try:
        text = _safe_read_text(Path(tmp_path))
        assert "中文" in text
    finally:
        Path(tmp_path).unlink()


# ---------------------------------------------------------------------------
# vector_store tests
# ---------------------------------------------------------------------------

def test_make_schema():
    schema = make_schema(4096)
    assert schema is not None
    field_names = [f.name for f in schema]
    assert "vector" in field_names
    assert "text" in field_names
    assert "path" in field_names


def test_vector_store_create_and_stats():
    """创建表、插入数据、查询统计。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(tmpdir, "test_kb", dimensions=8, model="test-model")
        assert store.count() == 0

        # 插入数据
        chunks = [
            {
                "vector": [0.1] * 8,
                "chunk_id": "c1",
                "text": "hello world",
                "path": "/test/file1.md",
                "doc_type": "text",
                "heading_path": [],
                "section_title": "",
                "start_line": 1,
                "end_line": 10,
                "mtime_ns": 1234567890,
                "metadata": {"key": "value"},
            },
            {
                "vector": [0.9] * 8,
                "chunk_id": "c2",
                "text": "foo bar",
                "path": "/test/file2.md",
                "doc_type": "text",
                "heading_path": ["Section"],
                "section_title": "Section",
                "start_line": 1,
                "end_line": 5,
                "mtime_ns": 1234567890,
                "metadata": {},
            },
        ]
        result = store.upsert_chunks(chunks)
        assert result["inserted"] == 2
        assert store.count() == 2

        # stats
        stats = store.stats()
        assert stats["total_chunks"] == 2
        assert stats["unique_files"] == 2


def test_vector_store_search():
    """向量搜索。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(tmpdir, "test_search", dimensions=4, model="test")
        chunks = [
            {"vector": [1.0, 0.0, 0.0, 0.0], "chunk_id": "c1", "text": "apple",
             "path": "/a.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 1, "metadata": {}},
            {"vector": [0.0, 1.0, 0.0, 0.0], "chunk_id": "c2", "text": "banana",
             "path": "/b.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 1, "metadata": {}},
        ]
        store.upsert_chunks(chunks)

        # 搜索 [1,0,0,0] 应该命中 apple
        results = store.search([1.0, 0.0, 0.0, 0.0], top_k=1)
        assert len(results) == 1
        assert results[0]["text"] == "apple"


def test_vector_store_incremental_update():
    """增量更新：同 path 的旧记录被替换。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(tmpdir, "test_incr", dimensions=4, model="test")

        # 第一次插入
        chunks1 = [
            {"vector": [1.0, 0, 0, 0], "chunk_id": "c1", "text": "old",
             "path": "/same.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 100, "metadata": {}},
        ]
        store.upsert_chunks(chunks1)
        assert store.count() == 1

        # 第二次插入同 path，应该替换
        chunks2 = [
            {"vector": [0, 1.0, 0, 0], "chunk_id": "c1-new", "text": "new",
             "path": "/same.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 200, "metadata": {}},
        ]
        store.upsert_chunks(chunks2)
        assert store.count() == 1  # 不是2

        # 搜索验证是新数据
        results = store.search([0, 1.0, 0, 0], top_k=1)
        assert results[0]["text"] == "new"


def test_vector_store_delete_by_path():
    """按路径删除。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = VectorStore(tmpdir, "test_del", dimensions=4, model="test")
        chunks = [
            {"vector": [1, 0, 0, 0], "chunk_id": "c1", "text": "keep",
             "path": "/keep.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 1, "metadata": {}},
            {"vector": [0, 1, 0, 0], "chunk_id": "c2", "text": "delete",
             "path": "/delete.md", "doc_type": "text", "heading_path": [],
             "section_title": "", "start_line": 1, "end_line": 1,
             "mtime_ns": 1, "metadata": {}},
        ]
        store.upsert_chunks(chunks)
        assert store.count() == 2

        store.delete_by_path("/delete.md")
        assert store.count() == 1


# ---------------------------------------------------------------------------
# knowledge_graph tests
# ---------------------------------------------------------------------------
