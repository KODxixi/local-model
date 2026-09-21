"""cli.py 表驱动单测：parser 路由、doctor 结构、错误 JSON、全局参数位置、chunk。

全部用 unittest.mock 把 RAGIndexer/RAGRetriever/VectorStore/subprocess/urllib
mock 掉，不依赖真实模型服务。
"""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cli
import pytest
from cli import (
    E_INVALID_ARGS,
    E_UNKNOWN_KB,
    build_parser,
    cmd_chunk,
    cmd_doctor,
    cmd_index,
)
from rag_indexer import KBConfig

# 所有顶层子命令
TOP_LEVEL_COMMANDS = [
    "index", "freshness", "stats", "retrieve", "search-image",
    "embed", "rerank", "doctor",
    "ingest", "chunk", "rewrite", "summary",
]


def _subparser_choices(ap: argparse.ArgumentParser) -> set[str]:
    """从 parser 里取出子命令名集合。"""
    for action in ap._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("no subparsers found")


def _fake_kb(name: str = "demo") -> KBConfig:
    return KBConfig(
        name=name,
        root=r"C:\nonexistent\root",
        type="text",
        patterns=["*.md"],
        dimensions=8,
        embed_model="test-embed",
    )


# ---------------------------------------------------------------------------
# 1. parser 路由
# ---------------------------------------------------------------------------

def test_build_parser_routes_all_commands():
    """表驱动：15 个顶层子命令都注册在 parser 上。"""
    ap = build_parser()
    choices = _subparser_choices(ap)
    assert choices == set(TOP_LEVEL_COMMANDS)
    assert len(choices) == len(TOP_LEVEL_COMMANDS)


@pytest.mark.parametrize("cmd", TOP_LEVEL_COMMANDS)
def test_each_command_parses(cmd: str):
    """每个子命令都能被 parse_args 识别（不抛 unrecognized）。"""
    ap = build_parser()
    # 各子命令的最小合法 argv
    minimal = {
        "index": [],
        "freshness": [],
        "stats": [],
        "retrieve": ["hello"],
        "search-image": ["x.png"],
        "ingest": ["f.md"],
        "chunk": ["f.md"],
        "rewrite": ["q"],
        "summary": ["f.md"],
        "embed": ["text"],
        "rerank": ["q", "--docs", "a", "b"],
        "migrate": ["--sqlite", "x.db"],
        "optimize": [],
        "doctor": [],
    }
    args = ap.parse_args([cmd, *minimal[cmd]])
    assert args.command == cmd


# ---------------------------------------------------------------------------
# 2. doctor 输出结构
# ---------------------------------------------------------------------------

def _fake_urlopen_factory():
    """按 URL 返回不同 JSON 的 urlopen mock。

    2026-09-21 甲-1：doctor 的检索探针与对话/视觉探针现在**都打 9123 的 /v1/models**
    （对话侧此前是 8080，已退役），同一 URL 无法再按端口区分，故返回全量模型列表。
    """
    def fake_urlopen(req, timeout=5):
        url = req.full_url
        body = b'{"data": []}'
        if "/v1/models" in url:
            body = json.dumps({"data": [
                {"id": "text-embedding-qwen3-embedding-8b"},
                {"id": "vl-reranker-2b"},
                {"id": "muse-glimmer-30b"},
            ]})
        elif "/running" in url:
            body = json.dumps(["text-embedding-qwen3-embedding-8b"])
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.read.return_value = body.encode("utf-8")
        return cm
    return fake_urlopen


def test_doctor_output_structure(capsys):
    """doctor --json 输出包含 dependencies/gpu/llama_swap/lm_studio/knowledge_bases/summary。"""
    args = SimpleNamespace(json=True, registry="registry.yaml", db="C:\\tmp\\lancedb")

    fake_registry = {"demo": _fake_kb()}
    store_mock = MagicMock()
    store_mock.stats.return_value = {"total_chunks": 42, "unique_files": 3}

    with patch.object(cli, "_check_deps", return_value={
            "lancedb": "ok", "pyarrow": "ok", "PIL": "ok",
            "yaml": "ok", "numpy": "ok", "fitz": "ok"}), \
         patch("subprocess.run", return_value=SimpleNamespace(
             returncode=0,
             stdout="NVIDIA RTX 5090,555,10,5000,32000",
             stderr="",
         )), \
         patch.object(cli.urllib.request, "urlopen", side_effect=_fake_urlopen_factory()), \
         patch.object(cli, "load_registry", return_value=fake_registry), \
         patch.object(cli, "create_vector_store", return_value=store_mock), \
         patch("rag_client.embed_texts", return_value=[[0.1] * 8]):
        rc = cmd_doctor(args)

    out = capsys.readouterr().out
    report = json.loads(out)
    # 顶层字段
    assert "checks" in report
    assert "summary" in report
    assert "knowledge_bases" in report
    # checks 下的子系统
    checks = report["checks"]
    for key in ("dependencies", "gpu", "llama_swap", "lm_studio", "lancedb", "registry_config"):
        assert key in checks, f"doctor 缺少 checks.{key}"
    # summary 结构
    s = report["summary"]
    assert set(s) >= {"total_checks", "passed", "failed", "warnings"}
    # 知识库条目
    assert "demo" in report["knowledge_bases"]
    assert report["knowledge_bases"]["demo"]["chunks"] == 42
    assert rc in (0, 1, 2)


def test_doctor_exit_code_on_missing_deps(capsys):
    """关键依赖缺失 → doctor 返回非 0 退出码。"""
    args = SimpleNamespace(json=True, registry="registry.yaml", db="C:\\tmp\\lancedb")
    fake_registry = {"demo": _fake_kb()}

    with patch.object(cli, "_check_deps", return_value={
            "lancedb": "missing: No module named 'lancedb'",
            "pyarrow": "ok"}), \
         patch("subprocess.run"), \
         patch.object(cli.urllib.request, "urlopen", side_effect=_fake_urlopen_factory()), \
         patch.object(cli, "load_registry", return_value=fake_registry), \
         patch.object(cli, "create_vector_store"):
        rc = cmd_doctor(args)

    assert rc != 0


# ---------------------------------------------------------------------------
# 3. 未知 KB 的统一 JSON 错误
# ---------------------------------------------------------------------------

def test_error_output_json_format(capsys):
    """未知 KB 时 stderr 输出 {"error": {code, message, fix}}。"""
    args = SimpleNamespace(
        json=True, registry="registry.yaml", db="C:\\tmp\\lancedb",
        kb="nope", force=False, no_prune=False, extract_entities=False,
    )
    with patch.object(cli, "load_registry", return_value={"real": _fake_kb()}):
        rc = cmd_index(args)

    assert rc == 1
    err = capsys.readouterr().err
    payload = json.loads(err)
    assert "error" in payload
    assert payload["error"]["code"] == E_UNKNOWN_KB
    assert "message" in payload["error"]
    assert "fix" in payload["error"]
    assert payload["error"]["kb"] == "nope"


# ---------------------------------------------------------------------------
# 4. 全局参数位置提示
# ---------------------------------------------------------------------------

def test_global_param_position_hint(capsys):
    """`retrieve "q" --kb x`（全局参数在子命令后）→ 友好提示 + exit 2。"""
    ap = build_parser()
    with pytest.raises(SystemExit) as exc:
        ap.parse_args(["retrieve", "hello", "--kb", "x"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    # usage 行含 {index,...}，JSON 错误块单独以 `{\n  "error"` 起始
    start = err.index('{\n  "error"')
    payload = json.loads(err[start:])
    assert payload["error"]["code"] == E_INVALID_ARGS
    assert "全局参数" in payload["error"]["fix"]
    assert "子命令之前" in payload["error"]["fix"]


# ---------------------------------------------------------------------------
# 5. chunk 命令
# ---------------------------------------------------------------------------

def test_chunk_command(tmp_path, capsys):
    """chunk 命令对 tmp md 文件输出分块 JSON。"""
    md = tmp_path / "doc.md"
    md.write_text(
        "# 标题\n\n这是一段足够长的正文内容，用来测试分块逻辑是否正常工作，"
        "包含足够字符以确保生成至少一个 chunk。\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(json=True, file=str(md), max_chars=800)
    rc = cmd_chunk(args)
    assert rc == 0
    out = capsys.readouterr().out
    chunks = json.loads(out)
    assert isinstance(chunks, list)
    assert len(chunks) >= 1
    assert "text" in chunks[0]
