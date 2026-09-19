"""Multimodal retrieval backend: llama-swap (Qwen3-VL GGUF via llama.cpp).

Talks to llama-swap's OpenAI-compatible endpoints (default 9123):
  POST /v1/embeddings   text:  {"input": ["..."]}
                        image: {"input": [{"prompt_string": "...<__media__>",
                                           "multimodal_data": ["<base64>"]}]}
  POST /v1/rerank       {"query": "...", "documents": [{"text": ...}]}
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from .lmstudio import _request_json

MEDIA_MARKER = "<__media__>"


MAX_IMAGE_DIM = 1152


def _image_base64(path: str) -> str:
    """llama.cpp multimodal_data 需要裸 base64。

    webp 等不支持格式转 PNG；超大/超长边图片降采样到 MAX_IMAGE_DIM，
    避免超过 llama-server 请求体上限或图像 token 上限。
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"图片不存在: {p}")
    suffix = p.suffix.lower()
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("图片预处理需要 Pillow，无法校验尺寸或缩图") from exc
    buffer = io.BytesIO()
    with Image.open(p) as image:
        if (
            suffix in {".png", ".jpg", ".jpeg", ".bmp"}
            and p.stat().st_size <= 5_000_000
            and max(image.size) <= MAX_IMAGE_DIM
        ):
            return base64.b64encode(p.read_bytes()).decode("ascii")
        image = image.convert("RGB")
        if max(image.size) > MAX_IMAGE_DIM:
            image.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
        image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class ImageRetrievalClient:
    def __init__(
        self,
        endpoint: str,
        embedding_model: str,
        reranker_model: str,
        dimensions: int,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.embedding_model = embedding_model
        self.reranker_model = reranker_model
        self.dimensions = dimensions

    def health(self) -> dict[str, Any]:
        payload = _request_json(self.endpoint, "/v1/models", timeout=10)
        ids = [m.get("id") for m in payload.get("data", [])] if isinstance(payload, Mapping) else []
        if self.embedding_model not in ids:
            raise RuntimeError(f"llama-swap 未注册模型 {self.embedding_model!r}")
        return {"status": "ready", "model": self.embedding_model, "dimensions": self.dimensions}

    def embed_image(self, path: str) -> list[float]:
        return self._embed_multimodal("", path)

    def embed_text(self, text: str) -> list[float]:
        payload = {"model": self.embedding_model, "input": [text.strip()]}
        return self._parse(_request_json(self.endpoint, "/v1/embeddings", payload, timeout=300))

    def embed_mixed(self, text: str, image: str) -> list[float]:
        return self._embed_multimodal(text.strip(), image)

    def _embed_multimodal(self, text: str, path: str) -> list[float]:
        prompt = f"{text} {MEDIA_MARKER}".strip() if text else f"Describe the image. {MEDIA_MARKER}".strip()
        payload = {
            "model": self.embedding_model,
            "input": [{"prompt_string": prompt, "multimodal_data": [_image_base64(path)]}],
        }
        return self._parse(_request_json(self.endpoint, "/v1/embeddings", payload, timeout=300))

    def _parse(self, response: Any) -> list[float]:
        data = response.get("data") if isinstance(response, Mapping) else None
        if not isinstance(data, list) or not data or "embedding" not in data[0]:
            raise RuntimeError("图文 embedding 返回格式错误")
        vector = data[0]["embedding"]
        if len(vector) != self.dimensions:
            raise RuntimeError(f"图文 embedding 维度 {len(vector)} != 配置 {self.dimensions}")
        return vector

    def rerank(
        self,
        query: str,
        documents: Sequence[Mapping[str, str]],
    ) -> list[dict[str, Any]]:
        """documents: list of {"text": ...}. 图片文档在 llama.cpp /v1/rerank 不支持，跳过。"""
        prepared: list[str] = []
        for doc in documents:
            text = (doc.get("text") or "").strip() if isinstance(doc, Mapping) else str(doc).strip()
            if text:
                prepared.append(text)
        if not prepared:
            raise ValueError("documents 必须含 text")
        response = _request_json(
            self.endpoint,
            "/v1/rerank",
            {
                "model": self.reranker_model,
                "query": query.strip(),
                "documents": prepared,
            },
            timeout=300,
        )
        results = response.get("results") if isinstance(response, Mapping) else None
        if not isinstance(results, list):
            raise RuntimeError("图文 rerank 返回格式错误")
        return [
            {"index": item.get("index"), "score": round(float(item.get("relevance_score", 0.0)), 6)}
            for item in results
        ]
