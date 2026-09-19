"""MCP routing contract; no real text-index or model access during unit tests.

P1 瘦身（2026-09-16）：server.py 不再 import 旧 SQLite indexer，故不再需要
patch indexer.Indexer。文本检索改走 Skill 层 RAGRetriever（在 test_server_tools.py 覆盖），
本文件只守图文库多路的路由/CLI 转发契约。

P2 优化（2026-09-16）：文本检索优先走 8765 常驻检索服务（_image_http_search），
以图搜图仍走 subprocess（8765 不支持 /search-image）。8765 不可用时 fallback subprocess。
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

spec = importlib.util.spec_from_file_location('image_mcp_test_server', BASE / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

# server._search_multimodal 期望 图文库配置含 project_root / python，
# 但 registry.yaml 只注册了 root/endpoint/dimensions 等元数据。
# 测试在 mock config 里补上这两个运行期字段（不改源码、不改 kbs.yaml）。
FAKE_IMAGE_ROOT = r"C:\path\to\image-cases"
FAKE_IMAGE_PROJECT = r"C:\path\to\image-lib"
FAKE_IMAGE_PYTHON = r"C:\path\to\image-lib\.venv\Scripts\python.exe"


def _image_config_with_runtime_fields() -> dict:
    """自包含的假图文库配置：用例不依赖本机注册表里恰好有这个库。"""
    return {
        "type": "multimodal",
        "root": FAKE_IMAGE_ROOT,
        "endpoint": "http://127.0.0.1:9123",
        "dimensions": 2048,
        "embed_model": "vl-embedding-2b",
        "reranker_model": "vl-reranker-2b",
        "project_root": FAKE_IMAGE_PROJECT,
        "python": FAKE_IMAGE_PYTHON,
        "cli_module": "image_lib",
        "capability": {"good_for": ["以图搜图"], "not_for": ["图片精排"]},
    }


class _FakeHTTPResponse:
    """模拟 8765 HTTP 响应的上下文管理器。"""
    def __init__(self, results: list):
        self._body = json.dumps({"query": "test", "results": results, "count": len(results),
                                 "time_s": 0.002, "cached": True}).encode("utf-8")
    def read(self) -> bytes:
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class ImageRetrievalSearchTests(unittest.TestCase):
    def test_list_kbs_exposes_registered_capabilities_without_inference(self):
        with patch.dict(server.KBS, {"image_lib": _image_config_with_runtime_fields()}), \
             patch.object(server.subprocess, 'run') as run:
            item = next(kb for kb in server.list_kbs() if kb['name'] == 'image_lib')
        self.assertIn('图片精排', item['capability']['not_for'])
        run.assert_not_called()

    def test_text_search_uses_8765_http_when_available(self):
        """文本检索优先走 8765 HTTP，不调用 subprocess。"""
        image_kb = _image_config_with_runtime_fields()
        fake_results = [{"case_name": "案例A", "score": 0.95}]
        with patch.dict(server.KBS, {"image_lib": image_kb}), \
             patch.object(server.urllib.request, 'urlopen', return_value=_FakeHTTPResponse(fake_results)) as http_mock, \
             patch.object(server.subprocess, 'run') as run:
            result = server.search('image_lib', '住宅')
        self.assertEqual(result, fake_results)
        http_mock.assert_called_once()
        run.assert_not_called()
        # 验证请求 URL 和 payload
        call_args = http_mock.call_args
        req = call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8765/search")

    def test_text_search_falls_back_to_subprocess_when_8765_down(self):
        """8765 不可用时，文本检索 fallback 到 subprocess。"""
        image_kb = _image_config_with_runtime_fields()
        cli_result = SimpleNamespace(returncode=0, stdout=json.dumps([{"case_name": "fallback"}]), stderr='')
        with patch.dict(server.KBS, {"image_lib": image_kb}), \
             patch.object(server.urllib.request, 'urlopen', side_effect=ConnectionError("8765 down")), \
             patch.object(server.subprocess, 'run', return_value=cli_result) as run:
            result = server.search('image_lib', '住宅')
        self.assertEqual(result, [{"case_name": "fallback"}])
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn('search', command)
        self.assertNotIn('--keyword', command)

    def test_keyword_mode_falls_back_to_subprocess_with_flag(self):
        """keyword 模式在 8765 不可用时，subprocess 应带 --keyword。"""
        image_kb = _image_config_with_runtime_fields()
        cli_result = SimpleNamespace(returncode=0, stdout=json.dumps([{"case_name": "kw"}]), stderr='')
        with patch.dict(server.KBS, {"image_lib": image_kb}), \
             patch.object(server.urllib.request, 'urlopen', side_effect=ConnectionError("8765 down")), \
             patch.object(server.subprocess, 'run', return_value=cli_result) as run:
            result = server.search('image_lib', '住宅', mode='keyword')
        self.assertEqual(result, [{"case_name": "kw"}])
        command = run.call_args.args[0]
        self.assertIn('--keyword', command)
        self.assertIn(FAKE_IMAGE_PROJECT, command)

    def test_image_search_always_uses_subprocess(self):
        """以图搜图：8765 不支持 /search-image，始终走 subprocess。"""
        image_kb = _image_config_with_runtime_fields()
        cli_result = SimpleNamespace(returncode=0, stdout=json.dumps([{"image": "result.webp"}]), stderr='')
        with patch.dict(server.KBS, {"image_lib": image_kb}), \
             patch.object(server.urllib.request, 'urlopen') as http_mock, \
             patch.object(server.subprocess, 'run', return_value=cli_result) as run:
            result = server.search('image_lib', '', image='query.jpg')
        self.assertEqual(result, [{"image": "result.webp"}])
        http_mock.assert_not_called()  # 以图搜图不走 HTTP
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn('search-image', command)
        self.assertIn('query.jpg', command)

    def test_invalid_modes_raise_before_any_call(self):
        """无效模式在调用任何后端前抛出 ValueError。"""
        image_kb = _image_config_with_runtime_fields()
        with patch.dict(server.KBS, {"image_lib": image_kb}), \
             patch.object(server.urllib.request, 'urlopen') as http_mock, \
             patch.object(server.subprocess, 'run') as run:
            for kwargs in ({'mode': 'invented'}, {'mode': 'keyword', 'image': 'image.png'}):
                with self.assertRaises(ValueError):
                    server.search('image_lib', '住宅', **kwargs)
            with self.assertRaises(ValueError):
                server.search('my_docs', '住宅', mode='keyword')
            http_mock.assert_not_called()
            run.assert_not_called()

    def test_http_search_uses_configured_retrieval_server_endpoint(self):
        """_image_http_search 从 cfg.retrieval_server 读取地址，不硬编码 8765。

        复现 codex 复验：registry.yaml 已有 retrieval_server 字段，但原实现
        硬编码 http://127.0.0.1:8765/search，配置未被消费。
        """
        custom_endpoint = "http://127.0.0.1:9999"
        image_kb = {
            **_image_config_with_runtime_fields(),
            "retrieval_server": custom_endpoint,
        }
        fake_resp = _FakeHTTPResponse([{"path": "/case.md", "score": 0.9}])
        captured_urls: list[str] = []

        def fake_urlopen(req, timeout=None):
            captured_urls.append(req.full_url)
            return fake_resp

        with patch.object(server.urllib.request, 'urlopen', side_effect=fake_urlopen):
            results = server._image_http_search(image_kb, "住宅", top_k=5)

        self.assertEqual(results, [{"path": "/case.md", "score": 0.9}])
        self.assertEqual(len(captured_urls), 1)
        self.assertEqual(captured_urls[0], f"{custom_endpoint}/search")

    def test_http_search_defaults_to_8765_when_config_missing(self):
        """cfg 缺少 retrieval_server 时回退默认 8765。"""
        image_kb = _image_config_with_runtime_fields()
        # 确保没有 retrieval_server 字段
        fake_resp = _FakeHTTPResponse([])

        with patch.object(server.urllib.request, 'urlopen', return_value=fake_resp) as http_mock:
            server._image_http_search(image_kb, "住宅", top_k=3)

        req = http_mock.call_args.args[0]
        self.assertEqual(req.full_url, "http://127.0.0.1:8765/search")


if __name__ == '__main__':
    unittest.main()
