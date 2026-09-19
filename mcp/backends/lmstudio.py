"""lms 模型管理（llama-swap / LM Studio 对话模型）。

P2 彻底瘦身（2026-09-16）：
  - 本文件**只**保留 lms CLI 模型管理函数（list_loaded_models / load_model /
    unload_model），这是 llama-swap/LM Studio 对话模型的运行期装卸，Skill 层
    没有对应管理 API，无法委托。
  - 原 LMStudioEmbedder 类已删除：embed 一律走 Skill 层 rag_client.embed_texts
    （server.py 直接调用，不再需要 MCP 层类包装）。
  - re-export ``_request_json``：backends/image_retrieval.py（不可改）仍
    ``from .lmstudio import _request_json`` 做图文 HTTP 重试，这是唯一兼容点。

红线：本文件**不做任何 embedding/rerank HTTP**。embed/rerank 只走 9123，
由 Skill 层 rag_client 实现。
"""

import json
import subprocess
from pathlib import Path
from typing import Any

# 共享指数退避重试：仅为 image_retrieval.py（不可改）re-export，本文件自身不发 HTTP。
from ._http import request_json as _request_json  # noqa: F401


def _lms_executable() -> str:
    candidate = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    return str(candidate if candidate.exists() else "lms")


def list_loaded_models() -> list[dict[str, Any]]:
    """列出当前加载到内存的模型（lms ps --json）。"""
    result = subprocess.run(
        [_lms_executable(), "ps", "--json"],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise RuntimeError(f"lms ps 失败: {result.stderr}")
    return json.loads(result.stdout)


def load_model(
    model: str,
    gpu: str = "auto",
    ttl: int | None = None,
    context_length: int | None = None,
    parallel: int = 1,
) -> dict[str, Any]:
    """装载模型。ttl 秒数用于闲置自动卸载。

    gpu：`max`（全卸载，推荐）、`off`（纯 CPU）、或 0~1 比例。
    `auto`（默认）不是 `lms load --gpu` 的合法值，这里省略 `--gpu` 让
    LM Studio 自动决定（20GB 级模型在 32GB 显存上会自动全卸载）。
    parallel：并发序列数，默认 1。顺序调用下 parallel=1 单请求生成最快；
    LM Studio UI 默认 4 会把单请求生成拖慢约 4 倍（实测 38→170 tok/s）。
    """
    cmd = [_lms_executable(), "load", model, "--identifier", model, "--yes"]
    if gpu and gpu != "auto":
        cmd += ["--gpu", gpu]
    if ttl:
        cmd += ["--ttl", str(ttl)]
    if context_length:
        cmd += ["--context-length", str(context_length)]
    if parallel:
        cmd += ["--parallel", str(parallel)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"lms load 失败: {result.stderr}")
    return {"model": model, "loaded": True, "gpu": gpu, "ttl_s": ttl, "parallel": parallel}


def unload_model(identifier: str = "all") -> dict[str, Any]:
    """卸载模型释放显存。identifier 传模型标识，或 'all' 卸载全部。"""
    cmd = [_lms_executable(), "unload"]
    if identifier == "all":
        cmd += ["--all"]
    else:
        cmd += [identifier]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError(f"lms unload 失败: {result.stderr}")
    return {"unloaded": identifier}
