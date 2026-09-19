"""rag_client 核心模块单测（全部 mock，不依赖 llama-swap）。

覆盖：
- embed_texts 空输入 / include_dimensions 返回结构
- _request_json 5xx 重试 5 次后抛异常；4xx 立即抛出不重试
- rerank_texts 空输入短路
- embed_images 单图预处理失败返回零向量
"""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest
import rag_client
from rag_client import embed_images, embed_texts, rerank_texts


def _http_error(code: int) -> urllib.error.HTTPError:
    """构造一个 urlopen 会抛出的 HTTPError。"""
    return urllib.error.HTTPError(
        url="http://127.0.0.1:9123/v1/embeddings",
        code=code,
        msg=f"mock {code}",
        hdrs=None,
        fp=None,
    )


# ---------------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------------

def test_embed_texts_empty_input():
    """空列表直接返回 []，不发请求。"""
    with patch.object(rag_client.urllib.request, "urlopen") as urlopen:
        assert embed_texts([]) == []
        urlopen.assert_not_called()


def test_embed_texts_empty_input_with_dimensions():
    """空列表 + include_dimensions=True 返回 ([], 0)。"""
    with patch.object(rag_client.urllib.request, "urlopen") as urlopen:
        result = embed_texts([], include_dimensions=True)
        assert result == ([], 0)
        urlopen.assert_not_called()


def test_embed_texts_include_dimensions():
    """include_dimensions=True 返回 (vectors, dims) 元组。"""
    payload = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ]
    }
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = __import__("json").dumps(payload).encode("utf-8")

    with patch.object(rag_client.urllib.request, "urlopen", return_value=cm) as urlopen:
        vectors, dims = embed_texts(
            ["a", "b"], include_dimensions=True, batch_size=2
        )
    assert dims == 3
    assert len(vectors) == 2
    # 按 index 排序后顺序正确
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert vectors[1] == [0.4, 0.5, 0.6]
    urlopen.assert_called_once()


def test_embed_texts_default_returns_list_only():
    """默认 include_dimensions=False 只返回向量列表（裸 list，不是 tuple）。"""
    payload = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read.return_value = __import__("json").dumps(payload).encode("utf-8")

    with patch.object(rag_client.urllib.request, "urlopen", return_value=cm):
        result = embed_texts(["a"])
    assert isinstance(result, list)
    assert result == [[0.1, 0.2]]


# ---------------------------------------------------------------------------
# _request_json 重试策略
# ---------------------------------------------------------------------------

def test_request_json_retry_on_5xx():
    """5xx 重试 5 次后抛 RuntimeError；sleep 被 mock 掉避免真实等待。"""
    with patch.object(rag_client.urllib.request, "urlopen", side_effect=_http_error(500)) as urlopen, \
         patch.object(rag_client.time, "sleep") as sleep, \
         pytest.raises(RuntimeError):
        rag_client._request_json("http://x/v1/embeddings", {"q": 1})
    # 初始 1 次 + 重试 4 次 = 5 次
    assert urlopen.call_count == 5
    # 每次失败都 sleep（4 次重试间隔）
    assert sleep.call_count == 4


def test_request_json_no_retry_on_4xx():
    """4xx 立即抛出，不重试、不 sleep。"""
    with patch.object(rag_client.urllib.request, "urlopen", side_effect=_http_error(400)) as urlopen, \
         patch.object(rag_client.time, "sleep") as sleep, \
         pytest.raises(urllib.error.HTTPError):
        rag_client._request_json("http://x/v1/embeddings", {"q": 1})
    urlopen.assert_called_once()
    sleep.assert_not_called()


# ---------------------------------------------------------------------------
# rerank_texts
# ---------------------------------------------------------------------------

def test_rerank_texts_empty():
    """空 query 或空 docs 直接返回 []，不发请求。"""
    with patch.object(rag_client, "_request_json") as req:
        assert rerank_texts("", ["doc1"]) == []
        assert rerank_texts("   ", ["doc1"]) == []
        assert rerank_texts("query", []) == []
    req.assert_not_called()


def test_rerank_texts_sorts_and_truncates():
    """按 relevance_score 降序，取 top_k。"""
    fake = {
        "results": [
            {"index": 0, "relevance_score": 0.2},
            {"index": 1, "relevance_score": 0.9},
        ]
    }
    with patch.object(rag_client, "_request_json", return_value=fake):
        results = rerank_texts("q", ["a", "b"], top_k=1)
    assert len(results) == 1
    assert results[0]["index"] == 1
    assert results[0]["text"] == "b"
    assert results[0]["score"] > 0.5


# ---------------------------------------------------------------------------
# embed_images 失败隔离
# ---------------------------------------------------------------------------

def test_embed_images_zero_vector_on_failure():
    """单图预处理（Image.open）抛异常 → 该图位置返回 2048 维零向量，不发批量请求。"""
    from PIL import Image

    with patch.object(Image, "open", side_effect=OSError("cannot identify image file")), \
         patch.object(rag_client, "_request_json") as req:
        vectors = embed_images(["definitely-not-an-image.png"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 2048
    assert all(v == 0.0 for v in vectors[0])
    # 预处理全失败 → 无有效 data_url → 不发批量 embedding 请求
    req.assert_not_called()


def test_embed_images_empty_input():
    """空输入直接返回 []。"""
    with patch.object(rag_client, "_request_json") as req:
        assert embed_images([]) == []
    req.assert_not_called()
