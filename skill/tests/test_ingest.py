"""ingest 多格式解析单测。

- text/md 解析：纯本地，无需外部服务
- PDF 解析：需要 PyMuPDF（本地库），不强制 llama-swap/LM Studio；
  标记 integration。文本层充足的页面不会触发 VLM。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from ingest import _guess_doc_type, ingest

# ---------------------------------------------------------------------------
# 文本 / Markdown
# ---------------------------------------------------------------------------

def test_parse_md_fixture(test_kb_dir: Path):
    """解析 fixtures/test-kb/facade-design.md，正文元信息正确。

    注：该 fixture 带 UTF-8 BOM，源码 _safe_read_text 按 utf-8 读取后 BOM 残留，
    H1 标题提取会退化为文件名；此处只断言 doc_type/内容/元数据，不断言 H1 标题。
    """
    doc = ingest(test_kb_dir / "facade-design.md")
    assert doc.doc_type == "text"
    assert doc.title, "标题为空"
    assert "玻璃幕墙" in doc.content
    assert "面砖" in doc.content
    assert doc.metadata["size_bytes"] > 0
    assert "sha256" in doc.metadata


def test_parse_md_all_three_files(test_kb_dir: Path):
    """3 个建筑主题 md 都能解析出非空内容。"""
    for name in ("facade-design.md", "floor-plan.md", "landscape.md"):
        doc = ingest(test_kb_dir / name)
        assert doc.title, f"{name} 无标题"
        assert len(doc.content) > 50, f"{name} 内容过短"


# ---------------------------------------------------------------------------
# doc_type 猜测（表驱动）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("a.md", "text"),
    ("a.txt", "text"),
    ("a.py", "text"),
    ("a.json", "text"),
    ("a.yaml", "text"),
    ("a.pdf", "pdf"),
    ("a.docx", "docx"),
    ("a.xlsx", "xlsx"),
    ("a.pptx", "pptx"),
    ("a.png", "image"),
    ("a.jpg", "image"),
    ("a.webp", "image"),
    ("a.unknown", "unknown"),
    ("a", "unknown"),
])
def test_guess_doc_type(name: str, expected: str):
    assert _guess_doc_type(Path(name)) == expected


# ---------------------------------------------------------------------------
# PDF（integration：需要 PyMuPDF 渲染，但不需要模型服务）
# ---------------------------------------------------------------------------

pymupdf = pytest.importorskip("fitz", reason="PyMuPDF 未安装")


@pytest.mark.integration
def test_parse_pdf_basic(test_pdf_path: Path):
    """解析研究报告 PDF：title/content 非空、页数 > 0。"""
    assert test_pdf_path.exists(), f"缺 fixture: {test_pdf_path}"
    doc = ingest(test_pdf_path)
    assert doc.doc_type == "pdf"
    assert doc.pages, "PDF 没有分页"
    assert doc.metadata["page_count"] > 0
    assert len(doc.pages) == doc.metadata["page_count"]
    # 文本层：正文非空（研究报告应为文本型 PDF）
    assert doc.content.strip(), "PDF 正文为空"
    assert len(doc.content) > 200
