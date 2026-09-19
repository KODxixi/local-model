"""智能分块器：按文档结构分块，不是固定字符数。

核心策略：
1. 识别 markdown 标题层级（# / ## / ###），按章节分块
2. 保持代码块、表格完整（不截断）
3. 大章节按段落二次切分
4. 重叠策略：相邻 chunk 共享一个段落（不是固定字符 overlap）
5. 每个 chunk 携带元数据：标题路径、文档类型、页码、字符数

输出 Chunk：统一格式，含文本、元数据、位置信息。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """分块后的文本单元。"""
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_id: str = ""
    start_pos: int = 0
    end_pos: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典（含 char_count 字段）。"""
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "metadata": self.metadata,
            "start_pos": self.start_pos,
            "end_pos": self.end_pos,
            "char_count": len(self.text),
        }


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 标题正则
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
# 代码块正则
CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
# 表格行正则（markdown 表格）
TABLE_ROW_RE = re.compile(r"^\|.*\|$", re.MULTILINE)

# 默认参数
DEFAULT_MAX_CHARS = 1200  # 每个 chunk 最大字符数（软上限，代码块/表格不截断）
DEFAULT_MIN_CHARS = 100   # 小于此值的 chunk 会被合并到前一个
DEFAULT_OVERLAP_PARAGRAPHS = 1  # 相邻 chunk 共享的段落数
#: 一行文本中“表格行”占比超过此阈值则整段判定为 markdown 表格
TABLE_LINE_RATIO = 0.7


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _split_paragraphs(text: str) -> list[str]:
    """按空行分割段落。"""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _heading_level(line: str) -> int | None:
    """返回标题层级（1-6），非标题返回 None。"""
    m = HEADING_RE.match(line)
    return len(m.group(1)) if m else None


def _extract_heading_path(text: str, pos: int) -> list[str]:
    """提取 pos 位置之前的所有标题，形成标题路径。"""
    path: list[tuple[int, str]] = []
    for m in HEADING_RE.finditer(text):
        if m.start() > pos:
            break
        level = len(m.group(1))
        title = m.group(2).strip()
        # 移除同级及更深的标题
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, title))
    return [t for _, t in path]


def _is_code_block(text: str) -> bool:
    """判断文本是否主要是代码块。"""
    return text.count("```") >= 2 or text.strip().startswith("```")


def _is_table(text: str) -> bool:
    """判断文本是否主要是 markdown 表格。"""
    lines = text.strip().splitlines()
    if len(lines) < 2:
        return False
    table_lines = sum(1 for line in lines if TABLE_ROW_RE.match(line.strip()))
    return table_lines / len(lines) > TABLE_LINE_RATIO


# ---------------------------------------------------------------------------
# 核心分块逻辑
# ---------------------------------------------------------------------------

def chunk_markdown(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
    overlap_paragraphs: int = DEFAULT_OVERLAP_PARAGRAPHS,
    doc_metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """按 markdown 结构智能分块。

    Args:
        text: markdown 文本
        max_chars: 每个 chunk 最大字符数（软上限）
        min_chars: 小于此值的 chunk 合并到前一个
        overlap_paragraphs: 相邻 chunk 共享的段落数
        doc_metadata: 文档级元数据，合并到每个 chunk

    Returns:
        Chunk 列表
    """
    if not text.strip():
        return []

    base_meta = dict(doc_metadata or {})
    chunks: list[Chunk] = []

    # 策略1：按标题分割为章节
    sections = _split_by_headings(text)

    for section in sections:
        section_chunks = _chunk_section(
            section["text"],
            heading_path=section["heading_path"],
            max_chars=max_chars,
            min_chars=min_chars,
            overlap_paragraphs=overlap_paragraphs,
            start_pos=section["start_pos"],
        )
        chunks.extend(section_chunks)

    # 合并过小的 chunk
    chunks = _merge_small_chunks(chunks, min_chars)

    # 分配 chunk_id 和元数据
    for i, chunk in enumerate(chunks):
        chunk.chunk_id = f"chunk-{i:04d}"
        chunk.metadata.update(base_meta)
        chunk.metadata.setdefault("index", i)

    return chunks


def _split_by_headings(text: str) -> list[dict[str, Any]]:
    """按标题分割文本为章节。"""
    sections: list[dict[str, Any]] = []
    last_pos = 0
    heading_path: list[tuple[int, str]] = []

    for m in HEADING_RE.finditer(text):
        # 标题之前的内容
        if m.start() > last_pos:
            content = text[last_pos:m.start()].strip()
            if content:
                sections.append({
                    "text": content,
                    "heading_path": [t for _, t in heading_path],
                    "start_pos": last_pos,
                })

        # 更新标题路径
        level = len(m.group(1))
        title = m.group(2).strip()
        while heading_path and heading_path[-1][0] >= level:
            heading_path.pop()
        heading_path.append((level, title))
        last_pos = m.start()

    # 最后一个章节
    if last_pos < len(text):
        content = text[last_pos:].strip()
        if content:
            sections.append({
                "text": content,
                "heading_path": [t for _, t in heading_path],
                "start_pos": last_pos,
            })

    # 如果没有标题，整个文本作为一个章节
    if not sections:
        sections.append({"text": text.strip(), "heading_path": [], "start_pos": 0})

    return sections


def _handle_protected_block(
    chunks: list[Chunk],
    para: str,
    paragraphs: list[str],
    i: int,
    *,
    heading_path: list[str],
    start_pos: int,
    current_parts: list[str],
    current_len: int,
    current_start: int,
    pos_offset: int,
    overlap_paragraphs: int,
) -> tuple[list[str], int, int, int]:
    """处理一个不可拆分的代码块/表格段落。

    先 flush 积累段落并 priming overlap 窗口，再把该段单独成块。

    Returns:
        更新后的 ``(current_parts, current_len, current_start, pos_offset)``。
    """
    flushed = _flush_pending_chunk(
        chunks, current_parts,
        heading_path=heading_path, start_pos=start_pos,
        current_start=current_start, pos_offset=pos_offset,
    )
    if flushed:
        current_parts = []
        current_len = 0
        # overlap：保留最后几个段落
        current_parts, current_len = _prime_overlap_window(
            paragraphs, end_idx=i, overlap_paragraphs=overlap_paragraphs
        )
        current_start = pos_offset

    # 代码块/表格单独成块
    chunks.append(Chunk(
        text=para,
        metadata={
            "heading_path": heading_path,
            "content_type": "code" if _is_code_block(para) else "table",
        },
        start_pos=start_pos + pos_offset,
        end_pos=start_pos + pos_offset + len(para),
    ))
    pos_offset += len(para) + 2  # +2 for blank lines
    return current_parts, current_len, current_start, pos_offset


def _chunk_section(
    text: str,
    *,
    heading_path: list[str],
    max_chars: int,
    min_chars: int,
    overlap_paragraphs: int,
    start_pos: int,
) -> list[Chunk]:
    """对一个章节进行分块。"""
    # 如果章节本身不大，直接返回
    if len(text) <= max_chars:
        return [Chunk(
            text=text,
            metadata={"heading_path": heading_path, "section_title": heading_path[-1] if heading_path else ""},
            start_pos=start_pos,
            end_pos=start_pos + len(text),
        )]

    # 大章节：先尝试按代码块/表格保护，再按段落分
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return [Chunk(
            text=text,
            metadata={"heading_path": heading_path},
            start_pos=start_pos,
            end_pos=start_pos + len(text),
        )]

    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_len = 0
    current_start = 0
    pos_offset = 0

    for i, para in enumerate(paragraphs):
        # 代码块/表格不拆分
        if _is_code_block(para) or _is_table(para):
            current_parts, current_len, current_start, pos_offset = _handle_protected_block(
                chunks, para, paragraphs, i,
                heading_path=heading_path, start_pos=start_pos,
                current_parts=current_parts, current_len=current_len,
                current_start=current_start, pos_offset=pos_offset,
                overlap_paragraphs=overlap_paragraphs,
            )
            continue

        # 普通段落：检查是否超过上限
        if current_len + len(para) > max_chars and current_parts:
            _flush_pending_chunk(
                chunks, current_parts,
                heading_path=heading_path, start_pos=start_pos,
                current_start=current_start, pos_offset=pos_offset,
            )
            # overlap：保留最后几个段落
            current_parts, current_len = _prime_overlap_window(
                paragraphs, end_idx=i, overlap_paragraphs=overlap_paragraphs
            )
            current_start = pos_offset

        current_parts.append(para)
        current_len += len(para)
        pos_offset += len(para) + 2

    # 输出最后一个
    if current_parts:
        chunks.append(_make_chunk(current_parts, heading_path, start_pos + current_start, start_pos + pos_offset))

    return chunks


def _make_chunk(parts: list[str], heading_path: list[str], start: int, end: int) -> Chunk:
    text = "\n\n".join(parts)
    return Chunk(
        text=text,
        metadata={"heading_path": heading_path, "section_title": heading_path[-1] if heading_path else ""},
        start_pos=start,
        end_pos=end,
    )


def _prime_overlap_window(
    paragraphs: list[str],
    *,
    end_idx: int,
    overlap_paragraphs: int,
) -> tuple[list[str], int]:
    """取 end_idx 之前 overlap_paragraphs 个段落作为重叠窗口（首尾相连时共享上下文）。

    Returns:
        ``(窗口段落列表, 窗口字符数)``；overlap_paragraphs<=0 时返回空窗口。
    """
    if overlap_paragraphs <= 0:
        return [], 0
    window = paragraphs[max(0, end_idx - overlap_paragraphs):end_idx]
    return list(window), sum(len(p) for p in window)


def _flush_pending_chunk(
    chunks: list[Chunk],
    current_parts: list[str],
    *,
    heading_path: list[str],
    start_pos: int,
    current_start: int,
    pos_offset: int,
) -> bool:
    """把当前累积段落输出为一个 chunk；返回是否真的输出了（空累积返回 False）。"""
    if not current_parts:
        return False
    chunks.append(
        _make_chunk(current_parts, heading_path, start_pos + current_start, start_pos + pos_offset)
    )
    return True


def _merge_small_chunks(chunks: list[Chunk], min_chars: int) -> list[Chunk]:
    """合并过小的 chunk 到前一个。"""
    if len(chunks) <= 1:
        return chunks

    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and len(chunk.text) < min_chars:
            # 合并到前一个
            prev = merged[-1]
            prev.text = prev.text + "\n\n" + chunk.text
            prev.end_pos = chunk.end_pos
            prev.metadata.update(chunk.metadata)
        else:
            merged.append(chunk)
    return merged


# ---------------------------------------------------------------------------
# 通用分块入口（自动识别格式）
# ---------------------------------------------------------------------------

def chunk_document(
    content: str,
    *,
    doc_type: str = "text",
    max_chars: int = DEFAULT_MAX_CHARS,
    doc_metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """通用分块入口，根据文档类型选择策略。

    Args:
        content: 文档内容
        doc_type: 文档类型（text/pdf/docx/xlsx/pptx/image）
        max_chars: 最大字符数
        doc_metadata: 文档元数据

    Returns:
        Chunk 列表
    """
    if doc_type in ("text", "pdf", "docx", "pptx", "unknown"):
        # 这些类型的内容通常是 markdown 或纯文本
        return chunk_markdown(content, max_chars=max_chars, doc_metadata=doc_metadata)

    if doc_type == "xlsx":
        # Excel：按 sheet 分块
        return _chunk_spreadsheet(content, max_chars=max_chars, doc_metadata=doc_metadata)

    if doc_type == "image":
        # 图片：VLM 输出通常是一段描述，不分块或简单分块
        if len(content) <= max_chars:
            return [Chunk(text=content, metadata=doc_metadata or {}, chunk_id="chunk-0000")]
        return chunk_markdown(content, max_chars=max_chars, doc_metadata=doc_metadata)

    # 默认
    return chunk_markdown(content, max_chars=max_chars, doc_metadata=doc_metadata)


def _chunk_spreadsheet(
    content: str,
    *,
    max_chars: int,
    doc_metadata: dict[str, Any] | None = None,
) -> list[Chunk]:
    """Excel 内容分块：按 sheet 分割（markitdown 输出通常用 ## Sheet Name）。"""
    return chunk_markdown(content, max_chars=max_chars, doc_metadata=doc_metadata)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="智能分块器")
    ap.add_argument("file", help="输入文件路径（或 - 从 stdin 读取）")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.file == "-":
        text = sys.stdin.read()
    else:
        from pathlib import Path
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")

    chunks = chunk_markdown(text, max_chars=args.max_chars)

    if args.json:
        print(json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2))
    else:
        print(f"Total chunks: {len(chunks)}")
        for c in chunks:
            title = c.metadata.get("section_title", "(no title)")
            path = " > ".join(c.metadata.get("heading_path", []))
            print(f"  [{c.chunk_id}] {len(c.text)} chars | {path} | {c.text[:60]}...")
