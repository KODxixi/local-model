"""server.py 工具层单测：mock Skill 层 API，只校验参数校验与输出格式化。

不触网、不碰 LanceDB / llama-swap。Skill 层函数（embed_texts/rerank_texts/
RAGRetriever/RAGIndexer）全部 patch；图文库多路由 test_image_retrieval_* 覆盖。
用例自带假注册表（patch KBS），不依赖本机真实配置。
"""
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

spec = importlib.util.spec_from_file_location('server_tools_test_server', BASE / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def _fake_result(path="a/b.md", score=0.91, text="chunk body", ranked_by="rerank", meta=None):
    return SimpleNamespace(
        path=path, score=score, text=text, ranked_by=ranked_by,
        metadata=meta if meta is not None else {"start_line": 3, "end_line": 9},
    )


def _fake_multimodal_kb() -> dict:
    """假图文库配置：让用例不依赖本机注册表里真实存在的库。"""
    return {
        "type": "multimodal",
        "endpoint": "http://127.0.0.1:9123",
        "dimensions": 2048,
        "embed_model": "vl-embedding-2b",
        "reranker_model": "vl-reranker-2b",
        "capability": {"good_for": ["以图搜图"], "not_for": ["图片精排"]},
    }


def _fake_text_kb() -> dict:
    """假文本库配置（同上，用例自带注册表）。"""
    return {"type": "text", "root": "/tmp/kb", "dimensions": 4096, "capability": {}}


@contextmanager
def _kb_env(**kbs: dict):
    """把假注册表装进 server（KBS + KB_REGISTRY），用例不依赖本机真实配置。"""
    with patch.dict(server.KBS, kbs, clear=True), \
         patch.dict(server.KB_REGISTRY, {name: MagicMock() for name in kbs}, clear=True):
        yield


class EmbedToolTests(unittest.TestCase):
    def test_requires_text_or_image(self):
        with self.assertRaises(ValueError):
            server.embed()

    def test_text_embed_delegates_to_skill_client(self):
        with patch.object(server, "embed_texts", return_value=[[0.1, 0.2, 0.3]]) as emb:
            out = server.embed(text="hello")
        self.assertEqual(out["type"], "text")
        self.assertEqual(out["dimensions"], 3)
        self.assertEqual(out["vector"], [0.1, 0.2, 0.3])
        # 红线：base_url 必须指向 9123
        _, kwargs = emb.call_args
        self.assertIn("9123", kwargs["base_url"])
        self.assertEqual(kwargs["model"], server.EMBED_CFG["model"])

    def test_image_embed_goes_through_image_client(self):
        fake_client = MagicMock()
        fake_client.embed_image.return_value = [1.0, 2.0, 3.0, 4.0]
        with patch.object(server, "_get_image_client", return_value=fake_client):
            out = server.embed(image="D:/case/photo.png")
        self.assertEqual(out["type"], "multimodal")
        self.assertEqual(out["dimensions"], 4)
        fake_client.embed_image.assert_called_once_with("D:/case/photo.png")


class RerankToolTests(unittest.TestCase):
    def test_empty_query_rejected(self):
        with _kb_env(my_docs=_fake_text_kb()), self.assertRaises(ValueError):
            server.rerank("my_docs", "  ", ["a", "b"])

    def test_empty_candidates_returns_empty(self):
        with _kb_env(my_docs=_fake_text_kb()):
            self.assertEqual(server.rerank("my_docs", "q", []), [])

    def test_unknown_kb_rejected(self):
        with self.assertRaises(ValueError):
            server.rerank("no_such_kb", "q", ["a"])

    def test_text_rerank_delegates_to_skill_client(self):
        ranked = [{"index": 1, "score": 0.88, "text": "b"}]
        with _kb_env(my_docs=_fake_text_kb()), \
             patch.object(server, "rerank_texts", return_value=ranked) as rk:
            out = server.rerank("my_docs", "query", ["a", "b"], top_k=2)
        self.assertEqual(out, ranked)
        _, kwargs = rk.call_args
        self.assertEqual(kwargs["top_k"], 2)
        self.assertIn("9123", kwargs["base_url"])


class SearchToolTests(unittest.TestCase):
    def test_unknown_kb_rejected(self):
        with self.assertRaises(ValueError):
            server.search("no_such_kb", "q")

    def test_empty_text_query_rejected(self):
        with _kb_env(my_docs=_fake_text_kb()), self.assertRaises(ValueError):
            server.search("my_docs", "   ")

    def test_invalid_mode_rejected(self):
        with _kb_env(my_docs=_fake_text_kb()), self.assertRaises(ValueError):
            server.search("my_docs", "q", mode="nope")

    def test_text_search_formats_skill_results(self):
        retriever_inst = MagicMock()
        retriever_inst.retrieve.return_value = [
            _fake_result(path="docs/a.md", score=0.9, text="body-1", ranked_by="rerank"),
            _fake_result(path="docs/b.md", score=0.7, text="body-2", ranked_by="embedding"),
        ]
        fake_cls = MagicMock(return_value=retriever_inst)
        with _kb_env(my_docs=_fake_text_kb()), patch.object(server, "RAGRetriever", fake_cls):
            out = server.search("my_docs", "where to discuss", top_k=5)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["path"], "docs/a.md")
        self.assertEqual(out[0]["snippet"], "body-1")
        self.assertEqual(out[0]["ranked_by"], "rerank")
        self.assertEqual(out[0]["start_line"], 3)
        self.assertAlmostEqual(out[0]["score"], 0.9, places=6)
        retriever_inst.retrieve.assert_called_once()
        _, kwargs = retriever_inst.retrieve.call_args
        self.assertEqual(kwargs["top_k"], 5)


class IndexToolTests(unittest.TestCase):
    def test_multimodal_index_returns_note(self):
        with patch.dict(server.KBS, {"image_lib": _fake_multimodal_kb()}):
            out = server.index("image_lib")
        self.assertEqual(out["kb"], "image_lib")
        self.assertIn("图文库", out["note"])

    def test_text_index_delegates_to_skill_indexer(self):
        indexer_inst = MagicMock()
        indexer_inst.index.return_value = {"kb": "my_docs", "chunks_indexed": 42}
        fake_cls = MagicMock(return_value=indexer_inst)
        with _kb_env(my_docs=_fake_text_kb()), patch.object(server, "RAGIndexer", fake_cls):
            out = server.index("my_docs")
        self.assertEqual(out["chunks_indexed"], 42)
        _, kwargs = indexer_inst.index.call_args
        self.assertFalse(kwargs["force"])


class ListKbsAndStatusTests(unittest.TestCase):
    def test_list_kbs_exposes_capabilities(self):
        with patch.dict(server.KBS, {"image_lib": _fake_multimodal_kb()}):
            items = server.list_kbs()
        names = {i["name"] for i in items}
        self.assertIn("image_lib", names)
        entry = next(i for i in items if i["name"] == "image_lib")
        self.assertIn("图片精排", entry["capability"]["not_for"])
        self.assertEqual(entry["backend"], "外部图文库")

    def test_status_reports_text_backend_health(self):
        with patch.object(server, "embed_texts", return_value=[[0.1] * 4096]), \
             patch.object(server, "rerank_texts", return_value=[{"index": 0, "score": 1.0, "text": "x"}]):
            out = server.status()
        self.assertTrue(out["text_embedding"]["ok"])
        self.assertEqual(out["text_embedding"]["dimensions"], 4096)
        self.assertTrue(out["text_rerank"]["ok"])


if __name__ == '__main__':
    unittest.main()
