"""文件解析器：把任意杂乱文件解构成结构化文档。

核心原则：
- 文本层优先：PDF 用 PyMuPDF 提取文本层，有文本层的页面不需要 OCR
- 视觉理解兜底：文本不足的页面/图片用配置的 VLM 做 OCR + 描述 + 分类，一次调用完成
- 不依赖 PaddleOCR：VLM 同时做文字识别和内容理解，输出结构化元数据

支持格式：
- 文本：md/txt/py/js/ts/json/yaml/yml/html/htm/csv/ini/log 等
- PDF：PyMuPDF 文本层 + VLM 视觉理解（扫描件/图片页）
- Word：docx（markitdown + python-docx 元数据）
- Excel：xlsx/xls（markitdown + openpyxl 元数据）
- PPT：pptx（markitdown + python-pptx 分页）
- 图片：png/jpg/jpeg/bmp/webp（VLM 理解）
- 其他：markitdown 尝试

输出 Document：统一结构化格式，含元数据、全文（markdown）、分页（如有）。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import random
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Page:
    """分页内容（PDF / PPTX 等有多页结构的文档）。"""
    number: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Document:
    """解析后的统一文档结构。"""
    path: str
    title: str
    doc_type: str  # text / pdf / docx / xlsx / pptx / image / unknown
    mime_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    content: str = ""  # markdown 格式全文
    pages: list[Page] = field(default_factory=list)
    html: str = ""  # HTML 重组（PDF 页面 webp + 文本）
    parse_engine: str = ""  # 标记用了哪个解析器，便于排障

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典（pages 一并展开）。"""
        return {
            "path": self.path,
            "title": self.title,
            "doc_type": self.doc_type,
            "mime_type": self.mime_type,
            "metadata": self.metadata,
            "content": self.content,
            "pages": [{"number": p.number, "content": p.content, "metadata": p.metadata} for p in self.pages],
            "html": self.html,
            "parse_engine": self.parse_engine,
        }


# ---------------------------------------------------------------------------
# VLM 客户端（Muse Glimmer 30B via OpenAI-compatible API）
# ---------------------------------------------------------------------------

# 可用环境变量 LOCAL_RAG_VLM_BASE_URL 覆盖
VLM_BASE_URL = os.getenv("LOCAL_RAG_VLM_BASE_URL", "http://127.0.0.1:9123")
# 模型名通过环境变量配置，不写死本机路径
VLM_MODEL = os.getenv("LOCAL_RAG_VLM_MODEL", "muse-glimmer-30b")
VLM_TIMEOUT = 120  # 秒，冷加载可能慢

# VLM 生成参数
VLM_TEMPERATURE = 0.1       # 低温度：OCR/描述要求稳定输出
VLM_MAX_TOKENS = 8192       # 推理模型 reasoning_content 占大量 token，<4096 会导致 content 为空

# 文本文件探测 markdown 标题时向前扫描的行数
TEXT_HEADING_PROBE_LINES = 20
# 从 VLM 描述中提取图片标题时，首行超过此长度则不作为标题
IMAGE_TITLE_MAX_FIRST_LINE = 100
# 解析错误信息在 Document.metadata 中的截断长度
ERROR_MSG_TRUNCATE = 200
# PDF 页面描述写入图片库时的截断长度
PDF_DESC_TRUNCATE = 500

# VLM 重试配置（与 rag_enhance 一致：指数退避 + jitter）
VLM_RETRY_MAX_ATTEMPTS = 3
VLM_RETRY_INITIAL_DELAY = 2.0
VLM_RETRY_BACKOFF = 2.0
VLM_RETRY_MAX_DELAY = 30.0

# 图片预处理默认值
DEFAULT_IMAGE_MAX_DIM = 1152
DEFAULT_JPEG_QUALITY = 85
SMALL_IMG_JPEG_QUALITY = 90

# PDF 渲染默认值（DPI / WebP 质量 / 最大边长）
PDF_RENDER_DPI = 144
PDF_WEBP_QUALITY = 85
PDF_MAX_EDGE = 2400
WEBP_ENCODE_METHOD = 6        # Pillow WebP method=6 最慢但压缩率最好

# PDF 页面 VLM 增强提示词
PDF_VLM_PROMPT = (
    "这是一份PDF文档的某一页。请：1) 提取页面中所有文字（OCR）；"
    "2) 简要描述页面内容（图表/图片/布局）。用中文回答，先文字后描述。"
)
#: PDF 元数据中不保留的字段（格式信息与加密标记，对检索无价值）
PDF_META_SKIP_KEYS = ("format", "encryption")

# 文本文件最大读取字节数
DEFAULT_MAX_TEXT_BYTES = 5_000_000

# VLM 结果缓存（按文件 sha256 + 提示词）
_vlm_cache: dict[str, str] = {}


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _image_to_base64(path: Path, max_dim: int = DEFAULT_IMAGE_MAX_DIM) -> str:
    """图片转 base64，超大图降采样（与图文检索侧保持同一口径）。"""
    from PIL import Image
    buffer = io.BytesIO()
    with Image.open(path) as img:
        if max(img.size) > max_dim:
            converted = img.convert("RGB")
            converted.thumbnail((max_dim, max_dim))
            converted.save(buffer, format="JPEG", quality=DEFAULT_JPEG_QUALITY)
        else:
            # 小图直接转，保持格式
            if img.format in ("PNG", "JPEG"):
                return base64.b64encode(path.read_bytes()).decode("ascii")
            converted = img.convert("RGB")
            converted.save(buffer, format="JPEG", quality=SMALL_IMG_JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _vlm_post_chat(payload: dict[str, Any]) -> str:
    """向 OpenAI-compatible 端点发送一次 VLM chat 请求，指数退避重试。

    Args:
        payload: OpenAI chat completions 请求体。

    Returns:
        模型输出文本（已 strip）；重试全部失败返回空字符串（最后一次异常已记 stderr）。
        推理模型只取 content 字段，忽略 reasoning_content。
    """
    max_attempts = VLM_RETRY_MAX_ATTEMPTS
    delay = VLM_RETRY_INITIAL_DELAY
    text = ""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{VLM_BASE_URL}/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as resp:
                result = json.loads(resp.read())
            text = str(result["choices"][0]["message"]["content"]).strip()
            # 推理模型只取 content，忽略 reasoning_content（思考过程）
            break
        except Exception as exc:
            last_exc = exc
            text = ""
            if attempt < max_attempts:
                sleep_for = min(delay + random.uniform(0, delay * 0.3), VLM_RETRY_MAX_DELAY)
                print(
                    f"[ingest] VLM attempt {attempt}/{max_attempts} failed: "
                    f"{type(exc).__name__}, retry in {sleep_for:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(sleep_for)
                delay = min(delay * VLM_RETRY_BACKOFF, VLM_RETRY_MAX_DELAY)

    if not text and last_exc is not None:
        print(f"[ingest] VLM 最终失败: {type(last_exc).__name__}: {last_exc}",
              file=sys.stderr)
    return text


def vlm_understand_image(
    path: str | Path,
    *,
    prompt: str = "请详细描述这张图片的内容，包括：1) 图片中的所有文字（OCR）；2) 画面主要内容和对象；3) 图片类型（照片/截图/图表/手绘/文档等）。用中文回答。",
    use_cache: bool = True,
) -> str:
    """用 Muse Glimmer 30B 理解图片，返回文字描述（含 OCR）。

    P1-1 修复：
    - 指数退避重试 3 次（initial_delay=2s, backoff=2.0, + jitter），
      应对端点冷加载 / 偶发连接重置。
    - 重试仍失败时返回空字符串（而不是 "[VLM error] ..."），避免错误
      文本被当作页面内容索引进向量库。调用方可通过空字符串判断失败。

    Args:
        path: 图片路径
        prompt: 自定义提示词
        use_cache: 是否使用缓存（按文件 hash + prompt）

    Returns:
        VLM 生成的描述文本；失败时返回空字符串。
    """
    p = Path(path).expanduser().resolve()
    cache_key = f"{_file_sha256(p)}:{prompt}"
    if use_cache and cache_key in _vlm_cache:
        return _vlm_cache[cache_key]

    b64 = _image_to_base64(p)
    payload = {
        "model": VLM_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        "temperature": VLM_TEMPERATURE,
        "max_tokens": VLM_MAX_TOKENS,
    }

    text = _vlm_post_chat(payload)

    if use_cache:
        _vlm_cache[cache_key] = text
    return text


def vlm_ocr_image(path: str | Path) -> str:
    """仅提取图片中的文字（OCR 模式）。"""
    return vlm_understand_image(
        path,
        prompt="请精确提取这张图片中的所有文字，保持原始排版和顺序。不要添加描述或解释，只输出文字内容。如果图片中没有文字，输出[无文字]。",
    )


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TEXT_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".text", ".log", ".ini", ".cfg", ".conf",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".less",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".htm", ".csv",
    ".sql", ".sh", ".bat", ".ps1", ".r", ".go", ".rs", ".java", ".c", ".cpp", ".h",
    ".env", ".gitignore", ".dockerfile", ".vue", ".svelte", ".php", ".rb",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif"}

# PDF 页面文本不足阈值（低于此值认为是扫描件/图片页，需要 VLM）
PDF_TEXT_MIN_CHARS = 20


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _guess_doc_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return "text"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".docx", ".doc"}:
        return "docx"
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return "xlsx"
    if suffix in {".pptx", ".ppt"}:
        return "pptx"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return "unknown"


def _safe_read_text(path: Path, max_bytes: int = DEFAULT_MAX_TEXT_BYTES) -> str:
    """按常见编码依次探测解码文本，全部失败时 utf-8 replace 兜底。"""
    raw = path.read_bytes()[:max_bytes]
    for encoding in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # intentional: 编码探测的正常控制流，逐个尝试下一个候选编码
            continue
    return raw.decode("utf-8", errors="replace")


def _file_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "size_bytes": stat.st_size,
        "modified": stat.st_mtime,
        "created": stat.st_ctime,
        "extension": path.suffix.lower(),
        "sha256": _file_sha256(path),
    }


def _title_from_path(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip() or path.name


# ---------------------------------------------------------------------------
# 各格式解析器
# ---------------------------------------------------------------------------

def _parse_text(path: Path) -> Document:
    content = _safe_read_text(path)
    title = _title_from_path(path)
    for line in content.splitlines()[:TEXT_HEADING_PROBE_LINES]:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    return Document(
        path=str(path),
        title=title,
        doc_type="text",
        mime_type=mimetypes.guess_type(str(path))[0] or "text/plain",
        metadata=_file_metadata(path),
        content=content,
        parse_engine="native-text",
    )


def _parse_with_markitdown(path: Path, doc_type: str) -> Document:
    from markitdown import MarkItDown
    md = MarkItDown()
    result = md.convert(str(path))
    content = result.text_content or ""
    title = result.title or _title_from_path(path)
    metadata = _file_metadata(path)
    if hasattr(result, "metadata") and result.metadata:
        metadata.update(result.metadata)
    return Document(
        path=str(path),
        title=title,
        doc_type=doc_type,
        mime_type=mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        metadata=metadata,
        content=content,
        parse_engine="markitdown",
    )


def _render_pdf_page_webp(page: Any, page_num: int, cache_dir: Path) -> tuple[Path, bool]:
    """渲染单个 PDF 页面为 WebP（增量缓存，已存在则跳过）。

    Args:
        page: fitz.Page 对象。
        page_num: 页码（0-based），文件名用 1-based 三位编号。
        cache_dir: 本 PDF 的 WebP 缓存目录。

    Returns:
        ``(webp_path, rendered_now)``——缓存命中时 rendered_now=False。
    """
    from PIL import Image

    webp_path = cache_dir / f"page_{page_num + 1:03d}.webp"
    if webp_path.exists():
        return webp_path, False
    pix = page.get_pixmap(dpi=PDF_RENDER_DPI, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # 缩放大页面，控制传输/显存体积
    if max(img.size) > PDF_MAX_EDGE:
        scale = PDF_MAX_EDGE / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.LANCZOS,
        )
    img.save(webp_path, format="WEBP", quality=PDF_WEBP_QUALITY, method=WEBP_ENCODE_METHOD)
    return webp_path, True


def _enhance_pdf_text_with_vlm(
    text: str,
    webp_path: Path,
    page_meta: dict[str, Any],
) -> str:
    """文本层不足（扫描件/图片页）时用 VLM 补 OCR + 描述。

    VLM 失败保留原文本并在 page_meta 标记 vlm_failed/vlm_error，
    不向向量库写入错误文本。

    Returns:
        增强后的页面文本。
    """
    try:
        vlm_text = vlm_understand_image(str(webp_path), prompt=PDF_VLM_PROMPT)
        # P1-1: VLM 失败时返回空字符串，不再把 "[VLM error]" 索引进库。
        if vlm_text:
            page_meta["vlm_used"] = True
            page_meta["text_extractor"] = f"vlm-{VLM_MODEL}"
            return vlm_text
        page_meta["vlm_failed"] = True
    except Exception as exc:
        page_meta["vlm_error"] = str(exc)[:ERROR_MSG_TRUNCATE]
        page_meta["vlm_failed"] = True
    return text


def _apply_pdf_metadata(doc: Any, metadata: dict[str, Any], pdf_hash: str, cache_dir: Path) -> None:
    """把 PyMuPDF 文档级元数据（作者/标题/页数等）合并进 Document.metadata（原地）。"""
    if doc.metadata:
        for k, v in doc.metadata.items():
            if v and k not in PDF_META_SKIP_KEYS:
                metadata[f"pdf_{k}"] = str(v)
        if doc.metadata.get("title"):
            metadata["title"] = doc.metadata["title"]
    metadata["page_count"] = doc.page_count
    metadata["pdf_sha256"] = pdf_hash
    metadata["webp_cache_dir"] = str(cache_dir)


def _parse_pdf(path: Path) -> Document:
    """PDF 解析：PyMuPDF 文本层 + 页面 WebP 渲染 + HTML 重组。

    策略：
    1. PyMuPDF 提取每页文本层
    2. 每页渲染为 WebP（144 DPI, quality=85, max_edge=2400），缓存到 ~/.local-rag/pdf-cache/
    3. 文本 < PDF_TEXT_MIN_CHARS 的页面用 VLM 做 OCR + 描述
    4. HTML 重组：每页 = <img webp> + 文本内容，便于检索和展示
    5. 增量缓存：通过 source_pdf_sha256 验证，未变更则跳过渲染
    """
    import fitz  # PyMuPDF

    metadata = _file_metadata(path)
    pages: list[Page] = []
    full_parts: list[str] = []
    html_parts: list[str] = []
    text_poor_count = 0
    vlm_count = 0
    webp_rendered = 0

    # WebP 缓存目录：~/.local-rag/pdf-cache/<pdf_sha256>/
    pdf_hash = _file_sha256(path)
    cache_dir = Path.home() / ".local-rag" / "pdf-cache" / pdf_hash
    cache_dir.mkdir(parents=True, exist_ok=True)

    with fitz.open(str(path)) as doc:
        # PDF 元数据
        _apply_pdf_metadata(doc, metadata, pdf_hash, cache_dir)

        for page_num in range(doc.page_count):
            page = doc.load_page(page_num)
            text = page.get_text("text", sort=True).strip()
            page_meta: dict[str, Any] = {"text_extractor": "pymupdf"}

            # 渲染页面为 WebP（增量缓存）
            webp_path, rendered_now = _render_pdf_page_webp(page, page_num, cache_dir)
            webp_rendered += int(rendered_now)
            page_meta["webp_path"] = str(webp_path)

            # 文本不足，用 VLM 视觉理解
            if len(text) < PDF_TEXT_MIN_CHARS:
                text_poor_count += 1
                enhanced = _enhance_pdf_text_with_vlm(text, webp_path, page_meta)
                if enhanced != text:
                    vlm_count += 1
                text = enhanced

            pages.append(Page(number=page_num + 1, content=text, metadata=page_meta))
            full_parts.append(f"## Page {page_num + 1}\n\n{text}")
            # HTML 重组：图片 + 文本
            html_parts.append(
                f'<div class="pdf-page" data-page="{page_num + 1}">'
                f'<img src="{webp_path.as_uri()}" alt="Page {page_num + 1}" loading="lazy">'
                f'<div class="page-text">{text}</div>'
                f"</div>"
            )

    metadata["text_poor_pages"] = text_poor_count
    metadata["vlm_processed_pages"] = vlm_count
    metadata["webp_rendered"] = webp_rendered
    metadata["webp_cached"] = max(0, metadata.get("page_count", 0) - webp_rendered)

    title = metadata.get("title") or metadata.get("pdf_title") or _title_from_path(path)
    return Document(
        path=str(path),
        title=title,
        doc_type="pdf",
        mime_type="application/pdf",
        metadata=metadata,
        content="\n\n".join(full_parts),
        pages=pages,
        html="\n".join(html_parts),
        parse_engine=f"pymupdf+vlm+webp ({vlm_count}/{len(pages)} pages VLM, {webp_rendered} rendered)",
    )


def _parse_docx(path: Path) -> Document:
    doc = _parse_with_markitdown(path, "docx")
    try:
        from docx import Document as DocxDocument
        d = DocxDocument(str(path))
        cp = d.core_properties
        if cp.title:
            doc.title = cp.title
        doc.metadata.update({
            "author": cp.author,
            "created": str(cp.created) if cp.created else None,
            "modified": str(cp.modified) if cp.modified else None,
            "paragraph_count": len(d.paragraphs),
        })
    except Exception as exc:
        # 元数据增强失败不影响正文，仅记录
        print(f"[ingest] docx 元数据提取失败 {path.name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return doc


def _parse_xlsx(path: Path) -> Document:
    doc = _parse_with_markitdown(path, "xlsx")
    try:
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        sheets = []
        for name in wb.sheetnames:
            ws = wb[name]
            sheets.append({
                "name": name,
                "max_row": ws.max_row,
                "max_column": ws.max_column,
            })
        doc.metadata["sheets"] = sheets
        doc.metadata["sheet_count"] = len(sheets)
        wb.close()
    except Exception as exc:
        print(f"[ingest] xlsx 工作表元数据提取失败 {path.name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return doc


def _parse_pptx(path: Path) -> Document:
    doc = _parse_with_markitdown(path, "pptx")
    try:
        from pptx import Presentation
        prs = Presentation(str(path))
        pages = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    # P1-6 修复：之前错误地写成 `for para in slide.shapes`
                    # 导致外层 shape 被重复遍历、文本提取不全。
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            texts.append(t)
            pages.append(Page(number=i, content="\n".join(texts)))
        doc.pages = pages
        doc.metadata["slide_count"] = len(pages)
    except Exception as exc:
        print(f"[ingest] pptx 分页提取失败 {path.name}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return doc


def _parse_image(path: Path) -> Document:
    """图片解析：VLM 一次完成 OCR + 描述 + 分类。"""
    description = vlm_understand_image(path)
    metadata = _file_metadata(path)
    metadata["vlm_model"] = VLM_MODEL
    metadata["analysis_type"] = "ocr+description+classification"

    # 尝试从 VLM 输出中提取标题（第一行或第一个有意义的短语）
    title = _title_from_path(path)
    first_line = description.split("\n")[0].strip()
    if first_line and len(first_line) < IMAGE_TITLE_MAX_FIRST_LINE and not first_line.startswith("["):
        title = first_line

    return Document(
        path=str(path),
        title=title,
        doc_type="image",
        mime_type=mimetypes.guess_type(str(path))[0] or "image/*",
        metadata=metadata,
        content=description,
        parse_engine=f"vlm-{VLM_MODEL}",
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

_PARSERS = {
    "text": _parse_text,
    "pdf": _parse_pdf,
    "docx": _parse_docx,
    "xlsx": _parse_xlsx,
    "pptx": _parse_pptx,
    "image": _parse_image,
}


def ingest(path: str | Path, *, max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES) -> Document:
    """解析任意文件为结构化 Document。

    Args:
        path: 文件绝对路径
        max_text_bytes: 文本文件最大读取字节数

    Returns:
        Document 对象

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 不支持的格式且 markitdown 也无法解析
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {p}")

    doc_type = _guess_doc_type(p)
    parser = _PARSERS.get(doc_type)

    if parser:
        return parser(p)

    # unknown 类型：尝试 markitdown
    try:
        return _parse_with_markitdown(p, doc_type)
    except Exception as exc:
        try:
            doc = _parse_text(p)
            doc.doc_type = "unknown"
            doc.parse_engine = f"text-fallback (markitdown failed: {exc})"
            return doc
        except Exception:
            # intentional: markitdown 与纯文本两条路都失败才放弃；
            # 把原始 markitdown 异常链入 ValueError，不静默吞掉
            raise ValueError(f"无法解析文件 {p}: {exc}") from exc


def ingest_batch(paths: list[str | Path]) -> list[Document]:
    """批量解析文件，单个失败不影响其他。"""
    results = []
    for p in paths:
        try:
            results.append(ingest(p))
        except Exception as exc:
            results.append(Document(
                path=str(p),
                title=_title_from_path(Path(p)),
                doc_type="error",
                mime_type="application/octet-stream",
                metadata={"error": str(exc)},
                content=f"[Parse error] {exc}",
                parse_engine="error",
            ))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="解析任意文件为结构化文档")
    ap.add_argument("path", help="文件路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-vlm", action="store_true", help="禁用 VLM（仅文本层提取）")
    args = ap.parse_args()

    if args.no_vlm:
        # 禁用 VLM：把阈值设到极大，跳过 VLM 调用
        PDF_TEXT_MIN_CHARS = 10_000_000

    doc = ingest(args.path)
    if args.json:
        print(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Title: {doc.title}")
        print(f"Type: {doc.doc_type} ({doc.mime_type})")
        print(f"Engine: {doc.parse_engine}")
        print(f"Metadata: {json.dumps(doc.metadata, ensure_ascii=False, default=str)[:500]}")
        print(f"Pages: {len(doc.pages)}")
        print(f"Content length: {len(doc.content)} chars")
        print("---")
        print(doc.content[:2000])
