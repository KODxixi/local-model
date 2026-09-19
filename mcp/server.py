"""local-models MCP：纯 stdio 适配层（P2 彻底瘦身，2026-09-16）。

本层**不含任何业务实现**，只做四件事：
  - stdio 协议解析（FastMCP SDK 自动处理）
  - 参数校验（类型 / 必填 / 范围）
  - 调用 Skill 层公共 API（skill 层的 scripts 目录）
  - 输出格式化（JSON）与错误转换

embed / rerank / 索引 / 检索**全部委托** Skill 层：
  - embed/rerank   -> rag_client.embed_texts / rerank_texts
  - 文本检索       -> rag_retriever.RAGRetriever
  - 文本建索引     -> rag_indexer.RAGIndexer / load_registry
  - 知识库配置     -> skill 层的 registry.yaml（唯一真相源）

backends/ 只保留 Skill 层没有对应物的三块：
  - image_retrieval 图文 HTTP 适配器（多模态 embed/rerank/health）
  - _http    共享指数退避重试（image_retrieval 依赖）
  - lmstudio  lms CLI 模型管理（list/load/unload 对话模型）
文本 embed/rerank 不再有 MCP 层类包装（LMStudioEmbedder / RerankerClient 已删除）。

红线（启动硬校验）：embed/rerank 必须走 llama-swap 9123，禁止 LM Studio 1234。
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp.server.fastmcp import FastMCP

# --- Skill 层 scripts 注入（embed/rerank/索引/检索的唯一实现所在）---
def _resolve_skill_scripts() -> Path:
    """定位 Skill 层 scripts 目录（不写死本机路径）。

    解析顺序：环境变量 ``LOCAL_MODEL_SKILL_PATH`` > 按仓库布局就近查找。
    兼容两种布局：``<root>/skill/scripts``（本仓库）与
    ``<root>/skills/<name>/scripts``（monorepo 形式）。
    """
    env = os.environ.get("LOCAL_MODEL_SKILL_PATH")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for base in here.parents[:3]:
        for rel in (Path("skill") / "scripts", Path("skills") / "local-model" / "scripts"):
            if (base / rel).is_dir():
                return base / rel
    return here.parents[1] / "skill" / "scripts"


SKILL_SCRIPTS = _resolve_skill_scripts()
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from rag_client import embed_texts, rerank_texts  # noqa: E402
from rag_indexer import RAGIndexer, load_registry  # noqa: E402
from rag_retriever import RAGRetriever  # noqa: E402

from backends.image_retrieval import ImageRetrievalClient
from backends.lmstudio import (
    list_loaded_models as _list_loaded,
    load_model as _load_model,
    unload_model as _unload_model,
)

BASE_DIR = Path(__file__).resolve().parent

# 配置唯一真相源：registry.yaml（Skill 层，在 scripts 的同级目录），kbs.yaml 仅作 deprecated fallback
REGISTRY_PATH = Path(
    os.environ.get("LOCAL_MODEL_REGISTRY_PATH", str(SKILL_SCRIPTS.parent / "registry.yaml"))
)
KBS_PATH = BASE_DIR / "kbs.yaml"  # deprecated

if REGISTRY_PATH.exists():
    _config = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    _config_source = "registry.yaml"
else:
    _config = yaml.safe_load(KBS_PATH.read_text(encoding="utf-8"))
    _config_source = "kbs.yaml (deprecated fallback)"

TEXT_BACKEND = _config["text_backend"]
EMBED_CFG = TEXT_BACKEND["embedding"]
RERANK_CFG = TEXT_BACKEND["rerank"]
KBS = _config.get("knowledge_bases") or {}  # 原始 dict：图文库运行期字段 + capability

# Skill 层 canonical KBConfig（文本检索 / 建索引用）。图文库多路仍用上面的原始 dict。
KB_REGISTRY: dict[str, Any] = (
    load_registry(REGISTRY_PATH) if REGISTRY_PATH.exists() else {}
)

# 硬校验：检索后端必须走 llama-swap 9123，禁止落到 LM Studio（1234）跑 embed/rerank。
# LM Studio 只跑对话/识图；把端点改指 1234 = 静默错轨，直接启动失败暴露。
for _role, _cfg in (("embedding", EMBED_CFG), ("rerank", RERANK_CFG)):
    _url = str(_cfg["base_url"])
    if "9123" not in _url:
        raise RuntimeError(
            f"local-models: text_backend.{_role} 指向 {_url}；embed/rerank 必须走 llama-swap 9123，"
            f"禁止 LM Studio 1234。请改 registry.yaml，勿在 LM Studio 跑检索。"
        )

# OCR 引擎：外部 OCR 脚本（独立 venv，确定性 det+rec）
# 默认按 monorepo 布局就近查找，可用环境变量覆盖
_OCR_PYTHON = Path(
    os.environ.get(
        "LOCAL_MODEL_OCR_PYTHON",
        str(BASE_DIR.parents[1] / "skills" / "pdf-toolkit" / ".venv" / "Scripts" / "python.exe"),
    )
)
_OCR_SCRIPT = Path(
    os.environ.get(
        "LOCAL_MODEL_OCR_SCRIPT",
        str(BASE_DIR.parents[1] / "skills" / "pdf-toolkit" / "scripts" / "ocr.py"),
    )
)

_image_client: ImageRetrievalClient | None = None


def _get_multimodal_kb() -> dict[str, Any]:
    """取注册表里第一个 ``type: multimodal`` 的库（不写死库名）。"""
    for cfg in KBS.values():
        if cfg.get("type") == "multimodal":
            return cfg
    raise ValueError(
        "注册表里没有 type: multimodal 的知识库；图文检索需要先注册一个（见 mcp/README.md）"
    )


def _get_image_client() -> ImageRetrievalClient:
    global _image_client
    if _image_client is None:
        cfg = _get_multimodal_kb()
        _image_client = ImageRetrievalClient(
            endpoint=cfg["endpoint"],
            embedding_model=cfg.get("embedding_model") or cfg.get("embed_model", "vl-embedding-2b"),
            reranker_model=cfg.get("reranker_model", "vl-reranker-2b"),
            dimensions=cfg["dimensions"],
        )
    return _image_client


def _get_kb(name: str) -> dict[str, Any]:
    if name not in KBS:
        raise ValueError(f"未知知识库 {name!r}；可用: {', '.join(KBS)}（用 list_kbs 查看）")
    return KBS[name]


mcp = FastMCP(
    "local-models",
    instructions=(
        "本地模型原子工具层。embed/rerank/ocr/模型管理是原子工具。"
        "文本检索/建索引委托 local-model Skill 的 RAG 层（rag_client + rag_retriever + rag_indexer，"
        "支持全格式解析、LanceDB ANN、查询改写）。"
        "配置唯一真相源：Skill 层的 registry.yaml。"
    ),
    log_level="ERROR",
)


@mcp.tool()
def list_kbs() -> list[dict[str, Any]]:
    """列出注册库与声明能力。配置来自 registry.yaml（kbs.yaml 已 deprecated）。"""
    return [
        {
            "name": name,
            "type": cfg["type"],
            "root": cfg.get("root", ""),
            "backend": "外部图文库" if cfg["type"] == "multimodal" else "llama-swap(文本)",
            "capability": cfg.get("capability", {}),
            "config_source": _config_source,
        }
        for name, cfg in KBS.items()
    ]


@mcp.tool()
def search(
    kb: str,
    query: str = "",
    top_k: int = 8,
    image: Optional[str] = None,
    mode: str = "hybrid",
) -> list[dict[str, Any]]:
    """在指定知识库中语义检索。

    - 文本库（如 my_docs）：query 文本检索，走 Skill 层 RAGRetriever
      （embed → LanceDB ANN 召回 → rerank 精排，rerank 失败自动降级 embedding 排序）。
    - 图文库（type: multimodal）：query 文本搜图；或传 image 路径以图搜图（走外部图文库 CLI）。
    - 图文库可显式 mode="keyword" 做快速 BM25 检索（不调用模型，非语义检索）。
    """
    cfg = _get_kb(kb)
    if mode not in {"hybrid", "keyword"}:
        raise ValueError("mode 必须为 hybrid 或 keyword")
    if mode == "keyword" and (cfg["type"] != "multimodal" or image):
        raise ValueError("keyword 仅支持图文库的文本关键词查询，不支持图片或文本库")
    if cfg["type"] == "text":
        return _search_text(kb, query, top_k)
    return _search_multimodal(cfg, query, top_k, image, keyword=mode == "keyword")


def _search_text(kb: str, query: str, top_k: int) -> list[dict[str, Any]]:
    """文本检索：委托 Skill 层 RAGRetriever，输出对齐旧结构。"""
    if not query.strip():
        raise ValueError("query 不能为空")
    rag_kb = KB_REGISTRY[kb]
    retriever = RAGRetriever(rag_kb, registry_path=str(REGISTRY_PATH))
    results = retriever.retrieve(query, top_k=top_k, mode="hybrid")
    return [
        {
            "path": r.path,
            "start_line": int((r.metadata or {}).get("start_line", 0) or 0),
            "end_line": int((r.metadata or {}).get("end_line", 0) or 0),
            "score": round(float(r.score), 6),
            "snippet": r.text,
            "ranked_by": r.ranked_by,
        }
        for r in results
    ]


def _search_multimodal(cfg: dict[str, Any], query: str, top_k: int, image: str | None, *, keyword: bool = False) -> list[dict[str, Any]]:
    """图文库检索：文本优先走 8765 常驻服务（带查询缓存），以图搜图/keyword 走 subprocess。

    8765 retrieval_server 优势：
    - 重复查询命中 LRU+SQLite 缓存，12s → 0.002s
    - 省去每次 ~2s Python 进程启动开销
    - 内部 embed/rerank 仍走 llama-swap 9123，不违反红线
    - 不支持以图搜图（无 /search-image 端点）和 keyword 模式（/search 是语义检索）
    """
    # 以图搜图 / keyword：8765 不支持，走 subprocess
    if image or keyword:
        return _image_subprocess_search(cfg, query, top_k, image, keyword=keyword)

    # 文本语义检索：优先 8765 HTTP，失败 fallback subprocess
    if not query.strip():
        raise ValueError("query 或 image 至少提供一个")

    try:
        return _image_http_search(cfg, query, top_k)
    except Exception as exc:
        print(f"[search] 8765 不可用，fallback subprocess: {exc}", file=sys.stderr)
        return _image_subprocess_search(cfg, query, top_k, image, keyword=keyword)


def _image_http_search(cfg: dict[str, Any], query: str, top_k: int) -> list[dict[str, Any]]:
    """调用图文库常驻检索服务，利用查询缓存。

    P0 修复（codex 审查）：地址从 registry.yaml 的 retrieval_server 字段读取，
    不再硬编码 8765。只支持语义文本检索；keyword/以图搜图走 subprocess。
    """
    endpoint = cfg.get("retrieval_server", "http://127.0.0.1:8765").rstrip("/")
    payload: dict[str, Any] = {"query": query, "top_k": top_k}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/search",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        result = json.loads(resp.read())
    return result.get("results", [])


def _image_subprocess_search(
    cfg: dict[str, Any], query: str, top_k: int, image: str | None, *, keyword: bool = False
) -> list[dict[str, Any]]:
    """通过 subprocess 调用外部图文库 CLI（常驻服务不可用时的 fallback）。"""
    project_root = cfg["project_root"]
    python = cfg["python"]
    cli_module = cfg.get("cli_module")
    if not cli_module:
        raise ValueError(
            "该图文库未配置 cli_module：subprocess fallback 需要知道调用哪个 CLI，"
            "例如在注册表里加 `cli_module: your-image-lib`"
        )
    cmd = [python, "-m", cli_module, "retrieve", "--root", project_root]
    if image:
        cmd += ["search-image", "--image", image, "--top-k", str(top_k)]
    else:
        cmd += ["search", "--query", query, "--top-k", str(top_k)]
        if keyword:
            cmd.append("--keyword")
    # The source checkout and bulk data live on different drives. Import the
    # current checkout explicitly; cases/runtime are junctions to the data drive.
    env = os.environ.copy()
    source_path = str(Path(project_root) / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_path, env.get("PYTHONPATH")]))
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        cmd, cwd=project_root, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=300, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"图文库检索失败: {result.stderr[-500:]}")
    return json.loads(result.stdout)


@mcp.tool()
def index(kb: str) -> dict[str, Any]:
    """为文本知识库建立/刷新语义索引，委托 Skill 层 RAGIndexer（LanceDB）。

    图文库（type: multimodal）索引由外部 CLI 管理，返回提示。
    """
    cfg = _get_kb(kb)
    if cfg["type"] == "multimodal":
        return {"kb": kb, "note": "图文库索引由外部图文库 CLI 管理，本工具不处理"}
    if cfg.get("path_filter"):
        return {"kb": kb, "note": f"该库复用 {cfg['root']} 的索引（path_filter={cfg['path_filter']}），无需单独建索引"}
    rag_kb = KB_REGISTRY[kb]
    return RAGIndexer(rag_kb, registry_path=str(REGISTRY_PATH)).index(force=False, prune=True)


@mcp.tool()
def rerank(kb: str, query: str, candidates: list[str], top_k: int = 5) -> list[dict[str, Any]]:
    """对候选文本做精排。文本库走检索端点，图文库走图文 reranker。"""
    cfg = _get_kb(kb)
    if not query.strip():
        raise ValueError("query 不能为空")
    if not candidates:
        return []
    if cfg["type"] == "text":
        return rerank_texts(
            query, candidates,
            model=RERANK_CFG["model"], base_url=RERANK_CFG["base_url"], top_k=top_k,
        )
    client = _get_image_client()
    docs = [{"text": c} for c in candidates]
    ranked = client.rerank(query, docs)
    return [
        {"index": item["index"], "score": item["score"], "text": candidates[item["index"]]}
        for item in ranked[: max(1, min(top_k, len(ranked)))]
    ]


@mcp.tool()
def embed(text: str = "", image: Optional[str] = None) -> dict[str, Any]:
    """生成 embedding 向量。传 text 走文本 embedding（9123）；传 image 走图文 embedding（image）。"""
    if image:
        client = _get_image_client()
        vector = client.embed_image(image) if not text.strip() else client.embed_mixed(text, image)
        return {"type": "multimodal", "dimensions": len(vector), "vector": vector}
    if not text.strip():
        raise ValueError("text 或 image 至少提供一个")
    vector = embed_texts([text], model=EMBED_CFG["model"], base_url=EMBED_CFG["base_url"])[0]
    return {"type": "text", "dimensions": len(vector), "vector": vector}


@mcp.tool()
def ocr(image_or_pdf: str, use_vlm: bool = False) -> dict[str, Any]:
    """用 PaddleOCR 本地识别图片/PDF 文字（确定性 det+rec，非生成式）。

    - image_or_pdf: 图片或 PDF 绝对路径；长图自动分块，PDF 按页 200dpi 渲染。
    - use_vlm: True 时用 PaddleOCR-VL（文档结构/表格感知）；缺省 PP-OCRv6。
    """
    if not Path(image_or_pdf).exists():
        raise ValueError(f"文件不存在: {image_or_pdf}")
    cmd = [str(_OCR_PYTHON), str(_OCR_SCRIPT), image_or_pdf, "--vlm"] if use_vlm \
        else [str(_OCR_PYTHON), str(_OCR_SCRIPT), image_or_pdf]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            timeout=600, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"OCR 失败: {(result.stderr or result.stdout)[-500:]}")
    return {
        "text": result.stdout,
        "engine": "PaddleOCR-VL" if use_vlm else "PP-OCRv6",
        "chars": len(result.stdout),
    }


@mcp.tool()
def status() -> dict[str, Any]:
    """检查所有后端的健康状态。文本 embed/rerank 走 Skill 层 rag_client。"""
    result: dict[str, Any] = {}
    # 文本 embedding
    try:
        v = embed_texts(
            ["health check"],
            model=EMBED_CFG["model"], base_url=EMBED_CFG["base_url"], timeout=10,
        )
        result["text_embedding"] = {"ok": True, "dimensions": len(v[0]) if v else 0}
    except Exception as exc:
        result["text_embedding"] = {"ok": False, "error": str(exc)[:200]}
    # 文本 rerank
    try:
        rerank_texts(
            "health", ["health check"],
            model=RERANK_CFG["model"], base_url=RERANK_CFG["base_url"], top_k=1, timeout=10,
        )
        result["text_rerank"] = {"ok": True}
    except Exception as exc:
        result["text_rerank"] = {"ok": False, "error": str(exc)[:200]}
    # 图文后端
    try:
        _get_image_client().health()
        result["multimodal"] = {"ok": True}
    except Exception as exc:
        result["multimodal"] = {"ok": False, "error": str(exc)[:200]}
    # OCR 引擎（PaddleOCR venv，文件存在性检查）
    result["ocr"] = {
        "ok": _OCR_PYTHON.exists() and _OCR_SCRIPT.exists(),
        "engine": "PaddleOCR (PP-OCRv6 / VL)",
        "python": str(_OCR_PYTHON),
    }
    return result


@mcp.tool()
def list_models() -> list[dict[str, Any]]:
    """列出当前加载到内存的本地模型及其显存占用、状态、TTL。"""
    return [
        {
            "identifier": m.get("identifier"),
            "model": m.get("displayName"),
            "size_gb": round(m.get("sizeBytes", 0) / 1e9, 2),
            "status": m.get("status"),
            "ttl_s": round(m["ttlMs"] / 1000) if m.get("ttlMs") else None,
        }
        for m in _list_loaded()
    ]


@mcp.tool()
def load_model(model: str, gpu: str = "auto", ttl: int = None, context_length: int = None, parallel: int = 1) -> dict[str, Any]:
    """装载本地模型。gpu 可选 max（全卸载，推荐）/off（纯 CPU）/0~1 比例；`auto`（默认）省略 --gpu 由 LM Studio 自动决定。parallel 并发序列数默认 1（顺序调用下最快，LM Studio UI 默认 4 会拖慢单请求约 4 倍）。ttl 秒数设置闲置自动卸载（情境化装卸的关键）。"""
    return _load_model(model, gpu=gpu, ttl=ttl, context_length=context_length, parallel=parallel)


@mcp.tool()
def unload_model(model: str = "all") -> dict[str, Any]:
    """卸载本地模型释放显存。model 传标识符，或默认 'all' 卸载全部。"""
    return _unload_model(model)


if __name__ == "__main__":
    mcp.run(transport="stdio")
