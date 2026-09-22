"""local-rag 统一 CLI 入口。

agent 只需调用这一个脚本，通过子命令访问所有功能：
  index / freshness / stats / retrieve / search-image / ingest / chunk /
  rewrite / summary / embed / rerank / doctor
  （2026-09-21 更正：此前这里还列着 migrate 与 optimize，但两者从未注册进 argparse，
    `--help` 里也没有 —— 文档与代码同时说谎。）

设计原则：
- 子命令层级清晰，agent 可预测
- 全局参数（--kb, --db, --registry）必须写在子命令**之前**（与 git/kubectl 一致），
  例如: cli.py --kb <kb名> retrieve "query"。写在子命令之后会报 unrecognized arguments。
- 所有子命令统一支持 --json（默认人类可读文本，--json 时输出 JSON）
- 错误输出统一为 {"error": {"code", "message", "command", "kb", "fix"}}
- doctor 命令一键诊断系统状态
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 延迟导入：依赖缺失时 doctor 仍可运行
# 关键依赖（lancedb/pyarrow）缺失时，这些 import 会失败，
# 但 doctor 命令不需要它们，可以先诊断依赖状态。
_IMPORT_ERRORS: dict[str, str] = {}
try:
    from rag_indexer import RAGIndexer, load_registry
except ImportError as e:
    _IMPORT_ERRORS["rag_indexer"] = str(e)
try:
    from rag_retriever import RAGRetriever, RetrievalResult
except ImportError as e:
    _IMPORT_ERRORS["rag_retriever"] = str(e)
try:
    from vector_store import (
        LanceDBVectorStore,
        VectorStore,
        VectorStoreProtocol,
        create_vector_store,
    )
except ImportError as e:
    _IMPORT_ERRORS["vector_store"] = str(e)
    VectorStore = None  # type: ignore[assignment,misc]
    LanceDBVectorStore = None  # type: ignore[assignment,misc]
    VectorStoreProtocol = None  # type: ignore[assignment,misc]
    create_vector_store = None  # type: ignore[assignment,misc]

DEFAULT_REGISTRY = SCRIPTS_DIR.parent / "registry.yaml"
DEFAULT_DB = "~/.local-rag/lancedb"

# 必须显式指定 --kb 的子命令（其余子命令与知识库无关）
_KB_REQUIRED_COMMANDS = frozenset(
    {"index", "freshness", "stats", "retrieve", "search-image", }
)

# ---------------------------------------------------------------------------
# 错误码（P1-2 统一错误格式；P4 扩充为完整枚举）
# ---------------------------------------------------------------------------
E_DEPENDENCY_MISSING = "E_DEPENDENCY_MISSING"   # 关键依赖未安装
E_CONNECTION_REFUSED = "E_CONNECTION_REFUSED"   # 模型服务连接失败
E_DIMENSION_MISMATCH = "E_DIMENSION_MISMATCH"  # 向量维度与表不符
E_UNKNOWN_KB = "E_UNKNOWN_KB"                    # 知识库未注册
E_INVALID_ARGS = "E_INVALID_ARGS"                # 参数缺失/非法
E_INTERNAL_ERROR = "E_INTERNAL_ERROR"            # 未分类运行时错误
# P4 新增
E_INDEX_LOCKED = "E_INDEX_LOCKED"                # 索引被另一进程锁定
E_FILE_PARSE_ERROR = "E_FILE_PARSE_ERROR"        # 文件解析失败
E_EMBEDDING_FAILED = "E_EMBEDDING_FAILED"        # embedding 调用失败
E_RERANK_FAILED = "E_RERANK_FAILED"             # rerank 调用失败
E_CONFIG_INVALID = "E_CONFIG_INVALID"            # registry/配置文件无效

#: 错误码 → 面向 Agent 的修复建议（main() 兜底分类时统一查表）
EXIT_OK = 0
EXIT_WARNING = 2   # doctor：存在 warning（注意：freshness 的 exit 2 表示索引过期，二者命令不同不冲突）
EXIT_ERROR = 1

GLOBAL_FLAGS = ("--kb", "--db", "--registry")


def _emit_error(
    code: str,
    message: str,
    *,
    command: str = "",
    kb: str = "",
    fix: str = "",
) -> None:
    """统一 JSON 错误输出到 stderr。

    形状：{"error": {"code", "message", "command", "kb", "fix"}}
    """
    payload = {
        "error": {
            "code": code,
            "message": message,
            "command": command,
            "kb": kb,
            "fix": fix,
        }
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


class _JsonArgumentParser(argparse.ArgumentParser):
    """argparse 错误也走统一 JSON 格式（P1-2）。

    同时检测全局参数位置陷阱（P0-2）：--kb/--db/--registry 写在子命令之后时
    给出友好提示。
    """

    def error(self, message: str) -> None:  # noqa: D401 - argparse API
        fix = ""
        if message.startswith("unrecognized arguments:"):
            tail = message[len("unrecognized arguments:"):].strip()
            tokens = tail.split()
            if tokens and tokens[0] in GLOBAL_FLAGS:
                fix = (
                    "全局参数 --kb/--db/--registry 必须写在子命令之前"
                    "（与 git/kubectl 一致）。"
                    '正确示例: cli.py --kb <kb名> retrieve "query"；'
                    '错误示例: cli.py retrieve "query" --kb <kb名>'
                )
        self.print_usage(sys.stderr)
        _emit_error(E_INVALID_ARGS, message, fix=fix)
        sys.exit(2)


def _add_json(p: argparse.ArgumentParser) -> None:
    """给子命令加统一的 --json flag（P1-1）。"""
    p.add_argument("--json", action="store_true", help="输出 JSON（默认人类可读文本）")


def _check_deps() -> dict[str, Any]:
    """检查关键依赖是否可导入（doctor 用，不依赖任何第三方库）。"""
    deps: dict[str, Any] = {}
    for mod in ["lancedb", "pyarrow", "PIL", "yaml", "numpy", "fitz"]:
        try:
            __import__(mod)
            deps[mod] = "ok"
        except ImportError as e:
            deps[mod] = f"missing: {e}"
    if _IMPORT_ERRORS:
        deps["_module_import_errors"] = _IMPORT_ERRORS
    return deps


def _require_deps(args: argparse.Namespace) -> None:
    """确保关键依赖可用，否则报错退出（非 doctor 命令用）。"""
    if _IMPORT_ERRORS:
        _emit_error(
            E_DEPENDENCY_MISSING,
            "依赖缺失，请先运行 setup.ps1 安装依赖",
            command=getattr(args, "command", ""),
            kb=getattr(args, "kb", ""),
            fix=f"cd {DEFAULT_REGISTRY.parent} && powershell -ExecutionPolicy Bypass -File setup.ps1",
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# 人类可读文本输出辅助（P1-1：--json 之外的默认输出）
# ---------------------------------------------------------------------------

def _dump(result: Any, as_json: bool) -> None:
    """根据 --json 决定输出 JSON 或原样 dump。"""
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------

def cmd_index(args: argparse.Namespace) -> int:
    """建立/更新索引。"""
    registry = load_registry(args.registry)
    if args.kb not in registry:
        _emit_error(E_UNKNOWN_KB, f"unknown kb '{args.kb}'",
                    command="index", kb=args.kb,
                    fix=f"可用知识库: {', '.join(registry)}")
        return 1
    kb = registry[args.kb]
    indexer = RAGIndexer(kb, db_path=args.db, registry_path=args.registry)
    result = indexer.index(force=args.force, prune=not args.no_prune)
    if args.json:
        _dump(result, True)
    else:
        print(f"KB: {result['kb']}  root: {result['root']}")
        print(f"发现 {result['files_discovered']} 文件，"
              f"索引 {result['files_indexed']}（跳过未变更 {result['files_skipped_unchanged']}），"
              f"失败 {result['files_failed']}")
        print(f"chunks: {result['chunks_indexed']}（库内共 {result['total_chunks_in_store']}），"
              f"孤儿清理: {result['orphan_files_pruned']}")
        print(f"耗时: {result['elapsed_seconds']}s")
        if result.get("errors"):
            print(f"错误（前 {len(result['errors'])}）:", file=sys.stderr)
            for e in result["errors"]:
                print(f"  - {e['path']}: {e['error']}", file=sys.stderr)
    return 2 if result["files_failed"] else 0


def cmd_freshness(args: argparse.Namespace) -> int:
    """检查索引新鲜度。"""
    registry = load_registry(args.registry)
    if args.kb not in registry:
        _emit_error(E_UNKNOWN_KB, f"unknown kb '{args.kb}'",
                    command="freshness", kb=args.kb,
                    fix=f"可用知识库: {', '.join(registry)}")
        return 1
    kb = registry[args.kb]
    indexer = RAGIndexer(kb, db_path=args.db, registry_path=args.registry)
    result = indexer.check_freshness()
    if args.json:
        _dump(result, True)
    else:
        status = "新鲜 ✅" if result["is_fresh"] else "过期 ⚠️"
        print(f"KB: {args.kb}  状态: {status}")
        print(f"文件系统 {result['total_files']} 个，已索引 {result['indexed_files']} 个")
        print(f"已修改 {result['modified_files']} / 新增 {result['new_files']} / 已删除 {result['deleted_files']}")
        if result.get("last_index_time"):
            print(f"最新索引时间: {result['last_index_time']}")
        if not result["is_fresh"]:
            print("运行 index 子命令更新索引", file=sys.stderr)
    return 0 if result.get("is_fresh", True) else 2


def cmd_stats(args: argparse.Namespace) -> int:
    """查看索引统计。"""
    registry = load_registry(args.registry)
    if args.kb not in registry:
        _emit_error(E_UNKNOWN_KB, f"unknown kb '{args.kb}'",
                    command="stats", kb=args.kb,
                    fix=f"可用知识库: {', '.join(registry)}")
        return 1
    kb = registry[args.kb]
    store = create_vector_store(
        backend=kb.vector_backend or "lancedb",
        db_path=args.db,
        kb_name=kb.name,
        dimensions=kb.dimensions,
        model=kb.embed_model,
    )
    result = store.stats()
    if args.json:
        _dump(result, True)
    else:
        print(f"KB: {args.kb}")
        for k, v in result.items():
            print(f"  {k}: {v}")
    return 0


def cmd_retrieve(args: argparse.Namespace) -> int:
    """混合检索 + rerank。支持 --kb all 跨库检索。"""
    registry = load_registry(args.registry)

    # 跨库检索
    if args.kb == "all":
        all_results: list[RetrievalResult] = []
        skipped: list[dict[str, str]] = []
        for name, kb in registry.items():
            # 2026-09-21：跨库检索**默认跳过 multimodal 库**。
            # 根因：multimodal 库的 embed_model 是 vl-embedding-2b，而两个向量组
            #   `exclusive: true` 互斥 —— 一次 `--kb all` 会驱逐常驻的 text-embedding（6.57 GB），
            #   全机文本检索进入 12.31s 冷加载窗口，直接吃掉上游 memory_search
            #   那个不可配的 30s 硬上限。而 `--kb all` 恰恰是文档推荐用法。
            # 要图检请显式 `--kb <图文库名>` —— 让"会换向量组"这件事是显式的。
            if getattr(kb, "type", "text") == "multimodal":
                skipped.append({
                    "kb": name,
                    "error": "multimodal：跨库检索默认跳过（避免驱逐文本向量组）。要图检请显式 --kb "
                             + name,
                })
                continue
            try:
                retriever = RAGRetriever(kb, db_path=args.db, registry_path=args.registry)
                results = retriever.retrieve(
                    args.query,
                    top_k=args.top_k,
                    mode=args.mode,
                    use_rerank=not args.no_rerank,
                    path_filter=args.path_filter,
                )
                for r in results:
                    r.metadata["kb"] = name
                all_results.extend(results)
            except Exception as e:
                skipped.append({"kb": name, "error": str(e)[:300]})
        # 按 score 降序，取 top_k
        all_results.sort(key=lambda r: r.score, reverse=True)
        all_results = all_results[: args.top_k]
        if args.json:
            print(json.dumps(
                {"results": [r.to_dict() for r in all_results], "skipped": skipped},
                ensure_ascii=False, indent=2,
            ))
        else:
            _print_results(all_results, False, show_explain=getattr(args, "explain", False))
            if skipped:
                print(f"\n⚠️ 跳过 {len(skipped)} 个知识库：", file=sys.stderr)
                for s in skipped:
                    print(f"  - {s['kb']}: {s['error']}", file=sys.stderr)
        return 0

    if args.kb not in registry:
        _emit_error(E_UNKNOWN_KB, f"unknown kb '{args.kb}'",
                    command="retrieve", kb=args.kb,
                    fix=f"可用知识库: {', '.join(registry)}（或用 --kb all 跨库）")
        return 1
    kb = registry[args.kb]
    retriever = RAGRetriever(kb, db_path=args.db, registry_path=args.registry)
    trace_output = getattr(args, "trace", None)
    results = retriever.retrieve(
        args.query,
        top_k=args.top_k,
        mode=args.mode,
        use_rerank=not args.no_rerank,
        path_filter=args.path_filter,
        trace=bool(trace_output),
        trace_output=trace_output,
    )
    _print_results(results, args.json, show_explain=getattr(args, "explain", False))
    if trace_output:
        print(f"\n📊 推理链已生成: {trace_output}", file=sys.stderr)
    return 0


def cmd_search_image(args: argparse.Namespace) -> int:
    """以图搜图：vl-embedding-2b 向量相似度检索。"""
    from rag_client import embed_images

    registry = load_registry(args.registry)
    if args.kb not in registry:
        _emit_error(E_UNKNOWN_KB, f"unknown kb '{args.kb}'",
                    command="search-image", kb=args.kb,
                    fix=f"可用知识库: {', '.join(registry)}")
        return 1
    kb = registry[args.kb]
    # 图片向量检索是 LanceDB 特有能力（search_images 不在 VectorStoreProtocol），
    # 直接用具体实现类。
    store = LanceDBVectorStore(args.db, kb.name, dimensions=kb.dimensions, model=kb.embed_model)

    # 生成查询图片的向量
    try:
        query_vectors = embed_images([args.image])
    except Exception as e:
        _emit_error(E_EMBEDDING_FAILED, f"图片embedding失败: {e}",
                    command="search-image", kb=args.kb)
        return 1
    if not query_vectors:
        _emit_error(E_EMBEDDING_FAILED, "图片embedding返回空",
                    command="search-image", kb=args.kb)
        return 1

    results = store.search_images(query_vectors[0], top_k=args.top_k)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            print(f"[{i}] {r['path']} (page={r['page_num']}, score={r['score']:.4f})")
            print(f"    webp: {r['metadata'].get('webp_path', 'N/A')}")
            print(f"    desc: {r['description'][:200]}")
            print()
    return 0


def _print_results(results: list[RetrievalResult], as_json: bool, show_explain: bool = False) -> None:
    """统一输出检索结果。"""
    if as_json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(results, 1):
            print(r.format_for_agent(i))
            if show_explain and r.explain:
                explain_str = "  🔍 " + " | ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in r.explain.items())
                print(explain_str)
            print()


def cmd_ingest(args: argparse.Namespace) -> int:
    """解析单个文件。"""
    from ingest import ingest
    doc = ingest(args.path)
    if args.json:
        print(json.dumps(doc.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Title: {doc.title}")
        print(f"Type: {doc.doc_type}")
        print(f"Pages: {len(doc.pages)}")
        print(f"Chars: {len(doc.content)}")
    return 0


def cmd_chunk(args: argparse.Namespace) -> int:
    """智能分块。"""
    # P0-1：chunker 无 chunk_text，实际为 chunk_document / chunk_markdown
    from chunker import chunk_document
    if args.file == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    chunks = chunk_document(text, max_chars=args.max_chars)
    if args.json:
        print(json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2))
    else:
        print(f"Total chunks: {len(chunks)}")
        for i, c in enumerate(chunks, 1):
            heading_path = c.metadata.get("heading_path", [])
            path_str = " > ".join(heading_path) if heading_path else "(无标题)"
            print(f"--- Chunk {i} ({len(c.text)} chars, {path_str}) ---")
            print(c.text[:200])
            print()
    return 0


def cmd_rewrite(args: argparse.Namespace) -> int:
    """查询改写。"""
    from rag_enhance import rewrite_query
    result = rewrite_query(args.query, context=args.context)
    payload = {
        "original": result.original,
        "rewritten": result.rewritten,
        "sub_queries": result.sub_queries,
        "keywords": result.keywords,
        "reasoning": result.reasoning,
    }
    if args.json:
        _dump(payload, True)
    else:
        print(f"原始: {payload['original']}")
        print(f"改写: {payload['rewritten']}")
        if payload["sub_queries"]:
            print(f"子查询: {'; '.join(payload['sub_queries'])}")
        if payload["keywords"]:
            print(f"关键词: {', '.join(payload['keywords'])}")
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """文档摘要。"""
    from rag_enhance import summarize_document
    content = Path(args.file).read_text(encoding="utf-8", errors="replace")
    result = summarize_document(content, title=args.title)
    payload = {
        "title": result.title,
        "summary": result.summary,
        "tags": result.tags,
        "key_entities": result.key_entities,
        "content_type": result.content_type,
        "language": result.language,
    }
    if args.json:
        _dump(payload, True)
    else:
        print(f"标题: {payload['title']}")
        print(f"类型: {payload['content_type']}  语言: {payload['language']}")
        print(f"摘要: {payload['summary']}")
        if payload["tags"]:
            print(f"标签: {', '.join(payload['tags'])}")
        if payload["key_entities"]:
            print(f"关键实体: {', '.join(payload['key_entities'])}")
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    """生成 embedding（调试用）。"""
    from rag_client import embed_texts
    vectors = embed_texts([args.text], model=args.model)
    payload = {"dimensions": len(vectors[0]), "first5": vectors[0][:5]}
    if args.json:
        _dump(payload, True)
    else:
        print(f"Dimensions: {payload['dimensions']}")
        print(f"First 5: {payload['first5']}")
    return 0


def cmd_rerank(args: argparse.Namespace) -> int:
    """精排（调试用）。"""
    from rag_client import rerank_texts
    result = rerank_texts(args.query, args.docs, top_k=args.top_k)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(result, 1):
            print(f"[{i}] score={r['score']:.4f} | {r['text'][:80]}")
    return 0




def cmd_doctor(args: argparse.Namespace) -> int:
    """系统诊断：依赖状态、GPU状态、模型可用性、embedding 冒烟、磁盘空间、配置校验、索引状态。"""
    import subprocess
    report: dict[str, Any] = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": {}}

    # 状态追踪（用于 summary 统计）
    # 每个检查项归为 ok / warning / error / unavailable
    statuses: list[tuple[str, str]] = []  # (name, status)

    def _mark(name: str, status: str) -> None:
        statuses.append((name, status))

    # 0. 依赖检查（最先执行，不依赖任何第三方库）
    deps = _check_deps()
    report["checks"]["dependencies"] = deps
    missing = [k for k, v in deps.items()
               if isinstance(v, str) and v.startswith("missing")]
    if missing:
        report["checks"]["dependencies"]["_summary"] = (
            f"缺失 {len(missing)} 个依赖: {', '.join(missing)}。"
            f"请运行 setup.ps1 安装。"
        )
        _mark("dependencies", "error")
    else:
        report["checks"]["dependencies"]["_summary"] = "所有依赖已安装"
        _mark("dependencies", "ok")

    # 1. GPU 硬件状态（nvidia-smi）
    gpu_status = "unavailable"
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            mem_used = int(parts[3]) if len(parts) > 3 else 0
            mem_total = int(parts[4]) if len(parts) > 4 else 0
            mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0
            gpu_entry: dict[str, Any] = {
                "status": "ok",
                "name": parts[0] if len(parts) > 0 else "unknown",
                "driver": parts[1] if len(parts) > 1 else "unknown",
                "utilization_pct": int(parts[2]) if len(parts) > 2 else 0,
                "memory_used_mib": mem_used,
                "memory_total_mib": mem_total,
                "memory_used_pct": round(mem_pct, 1),
            }
            # P0-3：显存使用率 >85% 加 warn
            if mem_pct > 85:
                gpu_entry["status"] = "warning"
                gpu_entry["warning"] = f"显存使用率 {mem_pct:.0f}% > 85%，模型冷加载可能 OOM"
                gpu_status = "warning"
            report["checks"]["gpu"] = gpu_entry
        else:
            report["checks"]["gpu"] = {"status": "error", "error": r.stderr.strip()}
            gpu_status = "error"
    except FileNotFoundError:
        report["checks"]["gpu"] = {"status": "unavailable", "error": "nvidia-smi not found"}
    except Exception as e:
        report["checks"]["gpu"] = {"status": "error", "error": str(e)}
        gpu_status = "error"
    _mark("gpu", gpu_status)

    # 2. 检查 llama-swap embedding 端点 + 当前加载模型
    # 端点不硬编码：embedding 走 rag_client.DEFAULT_BASE_URL（9123），
    # 对话/视觉端点走 rag_enhance.DEFAULT_LLM_BASE（默认 9123，仅对话/识图）。
    from rag_client import DEFAULT_BASE_URL
    try:
        from rag_enhance import DEFAULT_LLM_BASE
    except ImportError:
        DEFAULT_LLM_BASE = "http://127.0.0.1:9123"

    llama_swap_ok = True
    try:
        req = urllib.request.Request(f"{DEFAULT_BASE_URL}/v1/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            models = json.loads(resp.read())
        llama_swap_entry: dict[str, Any] = {
            "status": "ok",
            "endpoint": DEFAULT_BASE_URL,
            "registered_models": [m["id"] for m in models.get("data", [])],
        }
        # 检查当前加载的模型（/running 端点）
        try:
            req2 = urllib.request.Request(f"{DEFAULT_BASE_URL}/running")
            with urllib.request.urlopen(req2, timeout=5) as resp2:
                running = json.loads(resp2.read())
            llama_swap_entry["loaded_models"] = running if isinstance(running, list) else [running]
        except Exception:
            llama_swap_entry["loaded_models"] = "unknown (endpoint not available)"
        report["checks"]["llama_swap"] = llama_swap_entry
    except Exception as e:
        report["checks"]["llama_swap"] = {"status": "error", "error": str(e)}
        llama_swap_ok = False
    _mark("llama_swap", "ok" if llama_swap_ok else "error")

    # 2b. P0-3：实际 embedding 冒烟测试（验证模型可推理）
    if llama_swap_ok and not missing:
        try:
            from rag_client import embed_texts
            vecs = embed_texts(["doctor-health-check"], timeout=10)
            report["checks"]["embedding_smoke"] = {
                "status": "ok",
                "dimensions": len(vecs[0]) if vecs else 0,
            }
            _mark("embedding_smoke", "ok")
        except Exception as e:
            report["checks"]["embedding_smoke"] = {"status": "error", "error": str(e)[:300]}
            _mark("embedding_smoke", "error")
    else:
        report["checks"]["embedding_smoke"] = {
            "status": "skipped",
            "reason": "llama-swap 不可用或依赖缺失，跳过冒烟测试",
        }
        _mark("embedding_smoke", "ok")  # 跳过不算失败

    # 3. 检查对话/视觉端点（对话/识图，红线：不跑 embed/rerank）
    try:
        req = urllib.request.Request(f"{DEFAULT_LLM_BASE}/v1/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            models = json.loads(resp.read())
        report["checks"]["lm_studio"] = {
            "status": "ok",
            "endpoint": DEFAULT_LLM_BASE,
            "models": [m["id"] for m in models.get("data", [])],
        }
        _mark("lm_studio", "ok")
    except Exception as e:
        report["checks"]["lm_studio"] = {"status": "error", "error": str(e)}
        _mark("lm_studio", "error")

    # 4. 检查每个知识库的索引状态
    registry = load_registry(args.registry)
    report["knowledge_bases"] = {}
    for name, kb in registry.items():
        entry: dict[str, Any] = {"type": kb.type, "root": kb.root}
        try:
            store = create_vector_store(
                backend=kb.vector_backend or "lancedb",
                db_path=args.db,
                kb_name=name,
                dimensions=kb.dimensions,
                model=kb.embed_model,
            )
            stats = store.stats()
            entry["chunks"] = stats.get("total_chunks", 0)
            entry["files"] = stats.get("unique_files", 0)
            entry["index_exists"] = True
        except Exception as e:
            entry["index_exists"] = False
            entry["error"] = str(e)
        report["knowledge_bases"][name] = entry
        # 无索引视为 warning（库可能还没建索引），不算硬错误
        _mark(f"kb:{name}", "ok" if entry["index_exists"] else "warning")

    # 5. 检查 LanceDB 目录 + P0-3：磁盘剩余空间
    db_path = Path(args.db).expanduser()
    lancedb_entry: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
    }
    try:
        usage = shutil.disk_usage(str(db_path.parent if not db_path.exists() else db_path))
        free_gb = round(usage.free / (1024 ** 3), 1)
        total_gb = round(usage.total / (1024 ** 3), 1)
        lancedb_entry["disk_free_gb"] = free_gb
        lancedb_entry["disk_total_gb"] = total_gb
        if free_gb < 10:
            lancedb_entry["status"] = "warning"
            lancedb_entry["warning"] = f"磁盘剩余 {free_gb}GB < 10GB，索引可能写满"
            _mark("lancedb", "warning")
        else:
            lancedb_entry["status"] = "ok"
            _mark("lancedb", "ok")
    except Exception as e:
        lancedb_entry["status"] = "error"
        lancedb_entry["error"] = str(e)
        _mark("lancedb", "error")
    report["checks"]["lancedb"] = lancedb_entry

    # 6. P0-3：配置校验（registry.yaml 可解析且每个 KB 有必要字段）
    config_entry: dict[str, Any] = {"status": "ok", "kbs_loaded": len(registry)}
    config_problems: list[str] = []
    for name, kb in registry.items():
        if not kb.root:
            config_problems.append(f"{name}: 缺少 root")
        elif not Path(kb.root).expanduser().exists():
            config_problems.append(f"{name}: root 不存在 ({kb.root})")
        if not kb.dimensions:
            config_problems.append(f"{name}: 缺少 dimensions")
    if config_problems:
        config_entry["status"] = "warning"
        config_entry["problems"] = config_problems
        _mark("registry_config", "warning")
    else:
        _mark("registry_config", "ok")
    report["checks"]["registry_config"] = config_entry

    # P0-3：summary 字段
    failed = sum(1 for _, s in statuses if s == "error")
    warnings = sum(1 for _, s in statuses if s == "warning")
    passed = len(statuses) - failed - warnings
    # P4: 退出码语义明确化 — 0 全通过 / 2 有 warning / 1 有 error。
    # 注意 freshness 的 exit 2 表示"索引过期"，与本命令"有 warning"语义不同，命令名区分。
    exit_code = EXIT_ERROR if failed > 0 else (EXIT_WARNING if warnings > 0 else EXIT_OK)
    report["summary"] = {
        "total_checks": len(statuses),
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "exit_code": exit_code,
        "exit_code_meaning": {
            "0": "全部通过",
            "1": "存在 error（如依赖缺失/端点不可达）",
            "2": "无 error 但存在 warning（如未建索引/磁盘偏紧）",
        },
    }

    # 输出
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        s = report["summary"]
        print(f"Doctor 诊断 @ {report['timestamp']}")
        print(f"通过 {s['passed']} / 失败 {s['failed']} / 警告 {s['warnings']}（共 {s['total_checks']}）")
        print(f"退出码: {s['exit_code']}（0=通过 1=错误 2=有警告）")
        for name, entry in report["checks"].items():
            st = entry.get("status", "ok") if isinstance(entry, dict) else "ok"
            mark = {"ok": "✓", "warning": "⚠️", "error": "✗",
                    "unavailable": "?", "skipped": "·"}.get(st, "·")
            print(f"  {mark} {name}")
            if isinstance(entry, dict):
                for k in ("_summary", "warning", "error", "disk_free_gb", "loaded_models"):
                    if k in entry:
                        print(f"      {k}: {entry[k]}")
        print("知识库:")
        for name, kb in report["knowledge_bases"].items():
            mark = "✓" if kb.get("index_exists") else "⚠️"
            print(f"  {mark} {name}: chunks={kb.get('chunks', 0)} files={kb.get('files', 0)}")

    # P4 退出码：1=有 error，2=有 warning，0=全通过
    return exit_code


# ---------------------------------------------------------------------------
# CLI 构建
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = _JsonArgumentParser(
        prog="local-rag",
        description="本地 RAG 系统统一入口：索引、检索、文件解析",
        epilog=(
            "全局参数 --kb/--db/--registry/--json 必须写在子命令之前，例如:\n"
            "  cli.py --kb <kb名> retrieve \"查询\"\n"
            "  cli.py --json retrieve \"查询\" --top-k 3   # Agent 全局 JSON\n"
            "  cli.py doctor                              # 系统诊断（含已注册知识库）\n"
            "  cli.py --kb <kb名> index --force           # 全量重建索引"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="知识库注册表路径")
    ap.add_argument("--db", default=DEFAULT_DB, help="LanceDB 路径")
    ap.add_argument(
        "--kb",
        default=None,
        help="知识库名称（必填；retrieve 支持 'all' 跨库检索。已注册的库见 `cli.py doctor`）",
    )
    # P4 全局 --json：可写在子命令之前（Agent 统一契约）。与子命令级 --json 取 OR。
    ap.add_argument(
        "--json",
        dest="json_global",
        action="store_true",
        help="全局 JSON 输出（写在子命令前，Agent 统一用 --json；子命令级 --json 仍兼容）",
    )

    sub = ap.add_subparsers(dest="command", required=True)

    # index
    p = sub.add_parser("index", help="建立/更新索引")
    p.add_argument("--force", action="store_true", help="强制全量重建")
    p.add_argument("--no-prune", action="store_true", help="不清理孤儿 chunks")
    _add_json(p)
    p.set_defaults(func=cmd_index)

    # freshness
    p = sub.add_parser("freshness", help="检查索引新鲜度")
    _add_json(p)
    p.set_defaults(func=cmd_freshness)

    # stats
    p = sub.add_parser("stats", help="查看索引统计")
    _add_json(p)
    p.set_defaults(func=cmd_stats)

    # retrieve
    p = sub.add_parser("retrieve", help="混合检索 + rerank")
    p.add_argument("query", help="查询文本")
    p.add_argument("--top-k", type=int, default=8, help="返回结果数（默认 %(default)s）")
    p.add_argument("--mode", choices=["hybrid", "semantic", "keyword"], default="hybrid",
                   help="检索模式（默认 %(default)s）")
    p.add_argument("--no-rerank", action="store_true", help="跳过 rerank 精排（按召回分数排序）")
    p.add_argument("--path-filter", default=None, help="仅检索路径包含该子串的文档")


    p.add_argument("--trace", default=None, metavar="OUTPUT.html",
                   help="生成检索推理链可视化 HTML（查询→召回→融合→rerank→结果）")
    p.add_argument("--explain", action="store_true",
                   help="显示每个结果的详细评分（语义相似度/关键词匹配/RRF融合/rerank分数）")
    _add_json(p)
    p.set_defaults(func=cmd_retrieve)

    # search-image (以图搜图)
    p = sub.add_parser("search-image", help="以图搜图（vl-embedding-2b 向量检索）")
    p.add_argument("image", help="查询图片路径")
    p.add_argument("--top-k", type=int, default=5, help="返回结果数（默认 %(default)s）")
    _add_json(p)
    p.set_defaults(func=cmd_search_image)


    # ingest
    p = sub.add_parser("ingest", help="解析单个文件")
    p.add_argument("path", help="要解析的文件路径")
    _add_json(p)
    # P1-3：--no-vlm 已移除（ingest() 不支持该参数；VLM 是默认行为，
    # 文本层不足的 PDF 页/图片自动走配置的 VLM，见 SKILL.md 文件解析能力节）
    p.set_defaults(func=cmd_ingest)

    # chunk
    p = sub.add_parser("chunk", help="智能分块")
    p.add_argument("file", help="文件路径或 - 从 stdin")
    p.add_argument("--max-chars", type=int, default=800, help="每块最大字符数（默认 %(default)s）")
    _add_json(p)
    p.set_defaults(func=cmd_chunk)

    # rewrite
    p = sub.add_parser("rewrite", help="查询改写")
    p.add_argument("query", help="原始查询文本")
    p.add_argument("--context", default="", help="可选上下文（对话历史/知识库描述）")
    _add_json(p)
    p.set_defaults(func=cmd_rewrite)

    # summary
    p = sub.add_parser("summary", help="文档摘要")
    p.add_argument("file", help="要摘要的文件路径")
    p.add_argument("--title", default="", help="可选文档标题")
    _add_json(p)
    p.set_defaults(func=cmd_summary)

    # embed (调试)
    p = sub.add_parser("embed", help="生成 embedding（调试用）")
    p.add_argument("text", help="输入文本")
    p.add_argument("--model", default="text-embedding-qwen3-embedding-0.6b",
                   help="embedding 模型（默认 %(default)s）")
    _add_json(p)
    p.set_defaults(func=cmd_embed)

    # rerank (调试)
    p = sub.add_parser("rerank", help="精排（调试用）")
    p.add_argument("query", help="查询文本")
    p.add_argument("--docs", nargs="+", required=True, help="候选文档列表")
    p.add_argument("--top-k", type=int, default=5, help="返回结果数（默认 %(default)s）")
    _add_json(p)
    p.set_defaults(func=cmd_rerank)


    # optimize

    # doctor
    p = sub.add_parser("doctor", help="系统诊断：模型、索引、过期库")
    _add_json(p)
    p.set_defaults(func=cmd_doctor)

    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()

    # agent-md 铁律第 4 条：脚本自证，启动时打印 md5
    if not args.json:  # --json 时不打印，避免污染输出
        import hashlib as _hashlib
        from pathlib import Path
        skill_root = Path(__file__).resolve().parent.parent
        for _f in [
            "AGENTS.md",
            "SKILL.md",
            "rules/redlines.md",
            "rules/doctor-output.md",
            "system/pdf-vlm-prompt.md",
        ]:
            _p = skill_root / _f
            if _p.exists():
                _content = _p.read_bytes()
                _md5 = _hashlib.md5(_content).hexdigest()[:8]
                print(f"[local-model] {_f} md5={_md5} chars={len(_content.decode('utf-8'))}", file=sys.stderr)
    # P4: 全局 --json（子命令前）与子命令级 --json（子命令后）取 OR。
    # 二者 dest 不同（json_global / json），这里合并成统一的 args.json。
    args.json = bool(getattr(args, "json", False) or getattr(args, "json_global", False))
    if getattr(args, "command", None) in _KB_REQUIRED_COMMANDS and not args.kb:
        _emit_error(
            E_INVALID_ARGS,
            f"{args.command} 需要 --kb：请指定知识库名称",
            command=args.command,
            fix=f"cli.py --kb <kb名> {args.command} ...（已注册的库见 `cli.py doctor`）",
        )
        return 1
    # 依赖检查：doctor 命令不需要（它自己诊断依赖），其他命令需要
    if getattr(args, "command", None) != "doctor":
        _require_deps(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        # P1-2：统一错误 JSON 格式 + 错误码
        code = E_INTERNAL_ERROR
        msg = str(e)
        low = msg.lower()
        if "connection" in low or "refused" in low or "111" in low or "无法连接" in msg:
            code = E_CONNECTION_REFUSED
        elif "维度" in msg or "dimension" in low:
            code = E_DIMENSION_MISMATCH
        elif "锁" in msg or "locked" in low or "另一个索引进程" in msg:
            code = E_INDEX_LOCKED
        elif "embedding" in low and ("失败" in msg or "fail" in low):
            code = E_EMBEDDING_FAILED
        elif "rerank" in low:
            code = E_RERANK_FAILED
        elif isinstance(e, FileNotFoundError) or "无法解析" in msg or "parse" in low:
            code = E_FILE_PARSE_ERROR
        elif isinstance(e, (ValueError, KeyError)) and ("registry" in low or "config" in low or "配置" in msg):
            code = E_CONFIG_INVALID
        _emit_error(
            code,
            f"{type(e).__name__}: {msg}",
            command=getattr(args, "command", ""),
            kb=getattr(args, "kb", ""),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
