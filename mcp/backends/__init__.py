"""MCP backends：只保留 Skill 层没有对应物的异构适配。

image_retrieval  图文 HTTP 适配器（多模态 embed/rerank/health）
_http            共享指数退避重试（image_retrieval 依赖）
lmstudio         lms CLI 模型管理（list/load/unload）+ re-export _request_json

已删除：reranker.py（文本 rerank 走 rag_client.rerank_texts）、
LMStudioEmbedder（文本 embed 走 rag_client.embed_texts）。
"""

from ._http import request_json
from .image_retrieval import ImageRetrievalClient
from .lmstudio import list_loaded_models, load_model, unload_model

__all__ = [
    "ImageRetrievalClient",
    "request_json",
    "list_loaded_models",
    "load_model",
    "unload_model",
]
