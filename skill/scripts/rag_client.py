"""共享客户端：直接调用 llama-swap HTTP API 做 embedding/rerank。

直接通过标准库 HTTP 调用 llama-swap，延迟更低、更可控；脚本不依赖额外服务进程。

``POST /v1/embeddings`` 请求/响应协议（图片与文本共用）::

    请求: {"model": "...", "input": ["data:image/jpeg;base64,...", ...]}
    响应: {"data": [{"index": 0, "embedding": [0.1, ...]}, ...], ...}
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.request
from typing import Any

# llama-swap 统一端点（embed/rerank 红线：只允许检索端点，禁止指向对话后端）
# 可用环境变量 LOCAL_RAG_BASE_URL 覆盖（llama-swap 换端口时不用改代码）
DEFAULT_BASE_URL = os.getenv("LOCAL_RAG_BASE_URL", "http://127.0.0.1:9123")
DEFAULT_EMBED_URL = f"{DEFAULT_BASE_URL}/v1/embeddings"
DEFAULT_RERANK_URL = f"{DEFAULT_BASE_URL}/v1/rerank"
DEFAULT_TEXT_EMBED_MODEL = "text-embedding-qwen3-embedding-0.6b"
# 2026-09-21：原为 "text-reranker-8b"，该模型已从 llama-swap 摘除（与常驻 30B 算术冲突），
# 改由 vl-reranker-2b 兼任文本精排。**这是共享默认值** —— rag_retriever 的默认参数、
# cli.py 构造 RAGRetriever 时（不传 rerank_model）、本模块 CLI 的 --model 都走它，
# 改回旧名字会让 `cli.py retrieve` 的 rerank 步骤拿到 404（实测 "no router for requested model"）。
DEFAULT_TEXT_RERANK_MODEL = "vl-reranker-2b"
DEFAULT_VL_EMBED_MODEL = "vl-embedding-2b"
DEFAULT_VL_RERANK_MODEL = "vl-reranker-2b"

# 重试配置（适配 llama-swap TTL 卸载后的冷加载，~12s）
RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_DELAY = 2.0   # 秒
RETRY_MAX_DELAY = 30.0      # 秒
RETRY_BACKOFF = 2.0         # 指数退避倍率

# 调用默认参数（唯一来源，其他模块如需默认值应从此处 import，不要重复硬编码）
DEFAULT_TIMEOUT = 180            # 单次 HTTP 请求超时（秒，含重试）
DEFAULT_BATCH_SIZE = 64          # embedding 批大小（热态实测 35 docs/s 是甜点）
DEFAULT_RERANK_TOP_K = 5        # rerank 默认返回数
DEFAULT_HEALTH_TIMEOUT = 10     # 健康检查超时（秒）
DEFAULT_IMAGE_MAX_DIM = 1152    # 图片预处理最大边长（超过则等比缩放）
DEFAULT_JPEG_QUALITY = 85       # 图片 JPEG 编码质量 1-100
IMAGE_EMBEDDING_DIM = 2048      # vl-embedding-2b 输出维度（零向量长度依据）
HEALTH_ERROR_TRUNCATE = 200     # health_check 错误信息截断长度


def _request_json(
    url: str,
    payload: dict[str, Any],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    initial_delay: float = RETRY_INITIAL_DELAY,
) -> Any:
    """带指数退避重试的 JSON POST 请求。

    适配 llama-swap TTL 300s 自动卸载后的冷加载场景：
    第一次请求可能因模型卸载报 ConnectionReset (WinError 10054)，
    重试期间模型重新加载（~12s），第 2-3 次通常成功。

    重试策略：指数退避，initial_delay * backoff^(attempt-1)，上限 max_delay。
    仅对连接错误/超时/5xx 重试，4xx 立即抛出。
    """
    last_exc: Exception | None = None
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 4xx 不重试（除 429 Too Many Requests）
            if e.code == 429 or e.code >= 500:
                last_exc = e
            else:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # 连接重置/超时/网络错误 → 重试（适配 llama-swap 冷加载）
            last_exc = e
        except Exception:
            # 其他异常不重试
            raise

        if attempt < max_attempts:
            # P1-7: jitter — add up to 30% of current delay as random noise so
            # multiple clients don't retry in lock-step (thundering herd).
            sleep_for = min(delay + random.uniform(0, delay * 0.3), RETRY_MAX_DELAY)
            print(f"[rag_client] {url} attempt {attempt}/{max_attempts} failed: "
                  f"{type(last_exc).__name__}, retry in {sleep_for:.1f}s", file=sys.stderr)
            time.sleep(sleep_for)
            delay = min(delay * RETRY_BACKOFF, RETRY_MAX_DELAY)

    raise RuntimeError(
        f"请求失败（已重试 {max_attempts} 次）: {url} — {type(last_exc).__name__}: {last_exc}"
    ) from last_exc


def embed_texts(
    texts: list[str],
    *,
    model: str = DEFAULT_TEXT_EMBED_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    include_dimensions: bool = False,
) -> list[list[float]] | tuple[list[list[float]], int]:
    """批量生成文本 embedding。

    Args:
        texts: 文本列表
        model: 模型 ID
        base_url: llama-swap 端点
        timeout: 超时秒数（每个 batch，含重试）
        batch_size: 批大小（默认 64，热态实测 35 docs/s 是甜点；
                    原 16 只有 26 docs/s。registry.yaml performance.embed_batch_size
                    可覆盖此默认值。）
        include_dimensions: True 时返回 (vectors, dimensions) 元组，
                            便于调用方检测降级链维度变化（P1-2 修复）

    Returns:
        默认: 向量列表
        include_dimensions=True: (向量列表, 维度数)
    """
    if not texts:
        return ([], 0) if include_dimensions else []

    all_vectors: list[list[float]] = []
    dimensions = 0
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        payload = {"model": model, "input": batch}
        result = _request_json(f"{base_url}/v1/embeddings", payload, timeout=timeout)
        vectors = [item["embedding"] for item in sorted(result["data"], key=lambda x: x["index"])]
        if vectors and dimensions == 0:
            dimensions = len(vectors[0])
        all_vectors.extend(vectors)

    if include_dimensions:
        return all_vectors, dimensions
    return all_vectors


def _prepare_image_data_url(
    item: str,
    *,
    max_dim: int,
    jpeg_quality: int,
) -> str | None:
    """把单张图片输入预处理为 data URL（CPU 阶段，单图失败返回 None）。

    支持三种输入：已是 data URL、raw base64、本地文件路径（PIL 缩放+JPEG 编码）。

    Args:
        item: 图片输入（data URL / raw base64 / 文件路径）。
        max_dim: 图片最大边长（超过则等比缩放）。
        jpeg_quality: JPEG 编码质量 1-100。

    Returns:
        ``data:image/jpeg;base64,...`` 字符串；预处理失败返回 None。
    """
    import base64
    from io import BytesIO
    from pathlib import Path

    from PIL import Image

    # 已经是 data URL，直接用
    if item.startswith("data:"):
        return item
    # 尝试当作 raw base64（JPEG 以 /9j/ 开头，PNG 以 iVBOR 开头）
    try:
        # 快速校验：前 200 字符能被 base64 解码，且不是文件路径
        if not Path(item).exists() and not item.startswith(("C:", "D:", "c:", "d:")):
            base64.b64decode(item[:200], validate=True)
            return f"data:image/jpeg;base64,{item}"
    except Exception:
        # intentional: 探测性尝试，不是合法 base64 就回落到文件路径分支
        pass
    # 文件路径
    with Image.open(item) as img:
        img = img.convert("RGB")
        if max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"


def _embed_valid_image_batches(
    valid_indices: list[int],
    valid_urls: list[str],
    *,
    model: str,
    base_url: str,
    timeout: int,
    batch_size: int,
    result_vectors: list[list[float]],
) -> None:
    """按 batch_size 分组请求图片 embedding，把结果写回 result_vectors 对应下标（原地）。

    单个 batch 失败时该 batch 全部保持零向量，不影响其他 batch。
    """
    for batch_start in range(0, len(valid_urls), batch_size):
        batch_urls = valid_urls[batch_start : batch_start + batch_size]
        batch_indices = valid_indices[batch_start : batch_start + batch_size]
        try:
            payload = {"model": model, "input": batch_urls}
            resp = _request_json(f"{base_url}/v1/embeddings", payload, timeout=timeout)
            # 按 index 排序，确保顺序正确
            items = sorted(resp["data"], key=lambda x: x.get("index", 0))
            for idx, item in zip(batch_indices, items, strict=True):
                result_vectors[idx] = item["embedding"]
        except Exception as exc:
            # 整个 batch 请求失败时，该 batch 的所有图片保持零向量
            # （不重试，避免无限循环；调用方可根据零向量检测后单独重试）
            print(f"[embed_images] batch {batch_start}-{batch_start+len(batch_urls)} failed: {exc}",
                  file=sys.stderr)


def embed_images(
    image_inputs: list[str],
    *,
    model: str = DEFAULT_VL_EMBED_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT,
    max_dim: int = DEFAULT_IMAGE_MAX_DIM,
    batch_size: int = DEFAULT_BATCH_SIZE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> list[list[float]]:
    """批量生成图片 embedding（2048维，vl-embedding-2b）。

    修复记录（v2）:
    - P0 Bug A: 单图损坏隔离 — 预处理失败返回零向量，不拖垮整个 batch
    - P1 Bug B: GPU利用率优化 — 支持批量请求(batch_size=64)，减少HTTP overhead
    - P2 Bug C: API格式统一 — 兼容 file_path / data URL / raw base64 三种输入

    请求/响应协议见模块 docstring 中的 ``/v1/embeddings`` 说明。

    Args:
        image_inputs: 图片输入列表，支持三种格式:
                      - 文件路径: "path/to/photo.png"
                      - data URL: "data:image/jpeg;base64,/9j/4AAQ..."
                      - raw base64: "/9j/4AAQ..." (自动补全 data URL 前缀)
        model: 多模态嵌入模型 ID
        base_url: llama-swap 端点
        timeout: 超时秒数（每个 batch）
        max_dim: 图片最大边长（超过则等比缩放，减少传输体积）
        batch_size: 每批最大图片数（默认64，充分利用GPU）
        jpeg_quality: JPEG 编码质量 1-100

    Returns:
        2048维向量列表，与输入顺序一致。预处理失败的图片返回零向量（全0），
        调用方可通过 `all(v == 0 for v in vector)` 检测并过滤失败项。
    """
    if not image_inputs:
        return []

    ZERO_VECTOR = [0.0] * IMAGE_EMBEDDING_DIM

    # --- Phase 1: 预处理（CPU，逐张隔离失败） ---
    data_urls: list[str | None] = []
    for item in image_inputs:
        try:
            data_urls.append(
                _prepare_image_data_url(item, max_dim=max_dim, jpeg_quality=jpeg_quality)
            )
        except Exception as exc:
            # P0 Bug A 修复: 单图失败隔离，返回零向量标记
            data_urls.append(None)
            print(f"[embed_images] 预处理失败 {item[:80]}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    # --- Phase 2: 批量嵌入（GPU，按 batch_size 分组） ---
    valid_indices = [i for i, u in enumerate(data_urls) if u is not None]
    valid_urls = [data_urls[i] for i in valid_indices]

    # 初始化结果为零向量
    result_vectors: list[list[float]] = [ZERO_VECTOR[:] for _ in image_inputs]

    # --- Phase 2: 批量嵌入（GPU，按 batch_size 分组） ---
    _embed_valid_image_batches(
        valid_indices, valid_urls,
        model=model, base_url=base_url, timeout=timeout,
        batch_size=batch_size, result_vectors=result_vectors,
    )

    return result_vectors


def rerank_texts(
    query: str,
    documents: list[str],
    *,
    model: str = DEFAULT_TEXT_RERANK_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    top_k: int = DEFAULT_RERANK_TOP_K,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """对候选文本做精排。

    Args:
        query: 查询
        documents: 候选文本列表
        model: rerank 模型 ID
        base_url: llama-swap 端点
        top_k: 返回 top k
        timeout: 超时秒数

    Returns:
        [{"index": int, "score": float, "text": str}, ...]
    """
    if not query.strip() or not documents:
        return []

    payload = {"model": model, "query": query.strip(), "documents": documents}
    try:
        result = _request_json(f"{base_url}/v1/rerank", payload, timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"reranker 不可用 ({base_url}, timeout={timeout}s): {exc}") from exc

    ranked = sorted(result["results"], key=lambda x: x.get("relevance_score", 0), reverse=True)
    return [
        {
            "index": item["index"],
            "score": round(float(item.get("relevance_score", 0)), 6),
            "text": documents[item["index"]],
        }
        for item in ranked[: max(1, min(top_k, len(ranked)))]
    ]


def health_check(base_url: str = DEFAULT_BASE_URL, timeout: int = DEFAULT_HEALTH_TIMEOUT) -> dict[str, Any]:
    """检查 llama-swap 健康状态（embed + rerank 两个端点各发一次最小请求）。

    Args:
        base_url: llama-swap 端点。
        timeout: 单次检查超时秒数。

    Returns:
        ``{"embedding": {"ok": bool, "dimensions"| "error": str},
           "rerank": {"ok": bool, "error"?: str}}``；
        各端点独立失败，互不影响。
    """
    result: dict[str, Any] = {}
    # embedding
    try:
        v = embed_texts(["health check"], base_url=base_url, timeout=timeout)
        result["embedding"] = {"ok": True, "dimensions": len(v[0]) if v else 0}
    except Exception as exc:
        result["embedding"] = {"ok": False, "error": str(exc)[:HEALTH_ERROR_TRUNCATE]}
    # rerank
    try:
        rerank_texts("health", ["health check"], base_url=base_url, top_k=1, timeout=timeout)
        result["rerank"] = {"ok": True}
    except Exception as exc:
        result["rerank"] = {"ok": False, "error": str(exc)[:HEALTH_ERROR_TRUNCATE]}
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="embedding/rerank 客户端")
    sub = ap.add_subparsers(dest="command")

    p_embed = sub.add_parser("embed", help="生成 embedding")
    p_embed.add_argument("text", help="输入文本")
    p_embed.add_argument("--model", default=DEFAULT_TEXT_EMBED_MODEL)

    p_rerank = sub.add_parser("rerank", help="精排")
    p_rerank.add_argument("query", help="查询")
    p_rerank.add_argument("--docs", nargs="+", required=True, help="候选文档")
    p_rerank.add_argument("--model", default=DEFAULT_TEXT_RERANK_MODEL)
    p_rerank.add_argument("--top-k", type=int, default=DEFAULT_RERANK_TOP_K)

    p_health = sub.add_parser("health", help="健康检查")

    args = ap.parse_args()

    if args.command == "embed":
        vectors = embed_texts([args.text], model=args.model)
        print(f"Dimensions: {len(vectors[0])}")
        print(f"First 5: {vectors[0][:5]}")
    elif args.command == "rerank":
        results = rerank_texts(args.query, args.docs, model=args.model, top_k=args.top_k)
        for r in results:
            print(f"  [{r['index']}] score={r['score']:.4f} | {r['text'][:80]}")
    elif args.command == "health":
        print(json.dumps(health_check(), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
