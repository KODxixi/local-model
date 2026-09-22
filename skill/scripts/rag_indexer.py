"""RAG 索引编排器：ingest → chunk → embed → store。

完整流程：
1. 扫描知识库目录，发现文件
2. 增量检测（mtime_ns 对比，跳过未变更文件）
3. 调用 ingest.py 解析文件为 Document
4. 调用 chunker.py 智能分块
5. 调用 rag_client.py 生成 embedding
6. 写入 vector_store.py (LanceDB)
7. 孤儿清理（删除已不存在文件的 chunks）

配置驱动：每个知识库在 registry.yaml 中声明，新增库不改代码。
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# 确保 scripts 目录在 path 中
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from chunker import chunk_document
from ingest import ingest
from rag_client import embed_texts
from vector_store import VectorStoreProtocol, create_vector_store

# ---------------------------------------------------------------------------
# 性能配置（P0-4 / P1-8）：registry.yaml 顶层 performance 段
# ---------------------------------------------------------------------------

DEFAULT_PERF_CONFIG: dict[str, Any] = {
    "embed_batch_size": 64,
    "image_batch_size": 64,
    "rerank_recall_size": 24,
    "warmup_on_index": True,
}

# 索引返回/报告截断常量
MAX_REPORTED_ERRORS = 10   # index() 返回 errors 列表最多条数
MAX_STALE_REPORTED = 20    # check_freshness() 返回 stale_files 最多条数
DEFAULT_VL_IMAGE_MODEL = "vl-embedding-2b"  # PDF 页面图向量模型（图片库固定）
WARMUP_TIMEOUT = 60        # 预热 embedding 请求超时（秒）
ERROR_MSG_TRUNCATE = 300    # errors 列表中单条错误信息截断长度
PDF_PAGE_DESC_TRUNCATE = 500  # PDF 页面描述写入图片库的截断长度


def load_perf_config(registry_path: str | Path) -> dict[str, Any]:
    """读取 registry.yaml 的 performance 段，合并默认值。"""
    import yaml
    p = Path(registry_path)
    cfg = dict(DEFAULT_PERF_CONFIG)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        perf = data.get("performance") or {}
        for k, v in perf.items():
            cfg[k] = v
    except Exception as exc:
        print(f"[rag_indexer] 读取 performance 配置失败，用默认值: {exc}", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# 并发索引文件锁（P0-7）：Windows 上用 PID 文件检测，不需要 msvcrt.locking
# ---------------------------------------------------------------------------

LOCK_PATH = Path.home() / ".local-rag" / "index.lock"


class _IndexLock:
    """PID-based lock for `index()` so two concurrent runs can't corrupt LanceDB.

    Locks are advisory: if the lock file exists and the PID is alive, the
    second run refuses to start. On exit (including exception) the lock file
    is removed.
    """

    def __init__(self, lock_path: Path = LOCK_PATH) -> None:
        self.lock_path = lock_path
        self._acquired = False

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # ponytail: use the Windows read-only process API; os.kill(pid, 0)
            # sends a console control event on Windows and is not a no-op probe.
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
            kernel.GetExitCodeProcess.restype = ctypes.c_int
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return ctypes.get_last_error() == 5  # access denied: fail closed
            try:
                code = ctypes.c_ulong()
                ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                return not ok or code.value == 259  # STILL_ACTIVE; API failure: fail closed
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            # On Windows OSError can be raised for other reasons; treat as alive.
            return True

    def acquire(self) -> None:
        """获取索引锁：检查 PID 文件，活进程占用则拒绝；残留死 PID 则覆盖。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                content = self.lock_path.read_text(encoding="utf-8").strip()
                pid = int(content.splitlines()[0]) if content else 0
                if self._pid_alive(pid):
                    raise RuntimeError(
                        f"另一个索引进程正在运行 (PID={pid})，锁文件 {self.lock_path} 存在。"
                        f"如确认无进程，手动删除该文件后重试。"
                    )
                # 锁文件残留但 PID 已死，安全覆盖
            except ValueError:
                # intentional: 锁文件内容损坏/非数字 PID（如旧版本残留），
                # 按无活进程处理，直接覆盖
                pass
        self.lock_path.write_text(
            f"{os.getpid()}\n{datetime.now().isoformat()}\n",
            encoding="utf-8",
        )
        self._acquired = True

    def release(self) -> None:
        """释放索引锁（幂等：未获取或文件已外部删除均安全）。"""
        if self._acquired:
            try:  # noqa: SIM105 - 显式 except 便于保留 intentional 说明
                self.lock_path.unlink()
            except FileNotFoundError:
                # intentional: 锁文件已被外部删除（用户手动清理），幂等释放
                pass
            self._acquired = False

    def __enter__(self) -> _IndexLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

# ---------------------------------------------------------------------------
# 知识库注册表
# ---------------------------------------------------------------------------

@dataclass
class KBConfig:
    """知识库配置。"""
    name: str
    root: str
    type: str = "text"  # text / multimodal
    patterns: list[str] = field(default_factory=lambda: ["*.md", "*.txt", "*.py", "*.json", "*.yaml", "*.yml"])
    path_filter: str | None = None
    dimensions: int = 1024
    embed_model: str = "text-embedding-qwen3-embedding-0.6b"
    max_file_bytes: int = 5_000_000
    max_files: int = 10000
    # P0: 向量存储后端（lancedb / qdrant / chroma ...）。默认取 registry.yaml
    # 顶层 vector_store.type；每个 KB 可在自身配置里用 vector_backend 覆盖。
    vector_backend: str = "lancedb"

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> KBConfig:
        """从 registry.yaml 的单个 KB 配置字典构造 KBConfig（缺失项取默认值）。"""
        return cls(
            name=name,
            root=d["root"],
            type=d.get("type", "text"),
            patterns=d.get("patterns", ["*.md", "*.txt", "*.py", "*.json", "*.yaml", "*.yml"]),
            path_filter=d.get("path_filter"),
            dimensions=d.get("dimensions", 1024),
            embed_model=d.get("embed_model", "text-embedding-qwen3-embedding-0.6b"),
            max_file_bytes=d.get("max_file_bytes", 5_000_000),
            max_files=d.get("max_files", 10000),
        )


def load_registry(registry_path: str | Path) -> dict[str, KBConfig]:
    """加载知识库注册表（YAML）。

    同目录若存在 ``registry.local.yaml``（不进 git 的本机私有配置），
    其 ``knowledge_bases`` 会合并到主表之上，同名条目以本地文件为准。
    vector_backend 解析优先级：KB 自身 ``vector_backend`` > 顶层
    ``vector_store.type`` > 默认 ``"lancedb"``。
    """
    import yaml
    path = Path(registry_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    local_path = path.with_name(f"{path.stem}.local{path.suffix}")
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        data["knowledge_bases"] = {
            **(data.get("knowledge_bases") or {}),
            **(local.get("knowledge_bases") or {}),
        }
    default_backend = str((data.get("vector_store") or {}).get("type", "lancedb"))
    text_embedding = (data.get("text_backend") or {}).get("embedding") or {}
    default_dimensions = int(text_embedding.get("dimensions", 1024))
    default_embed_model = str(text_embedding.get("model", "text-embedding-qwen3-embedding-0.6b"))
    kbs = {}
    for name, cfg in (data.get("knowledge_bases") or {}).items():
        cfg = dict(cfg)
        if cfg.get("type", "text") == "text":
            cfg.setdefault("dimensions", default_dimensions)
            cfg.setdefault("embed_model", default_embed_model)
        kb = KBConfig.from_dict(name, cfg)
        kb.vector_backend = cfg.get("vector_backend", default_backend)
        kbs[name] = kb
    return kbs


# ---------------------------------------------------------------------------
# 索引器
# ---------------------------------------------------------------------------

class RAGIndexer:
    """RAG 索引编排器。"""

    def __init__(
        self,
        kb: KBConfig,
        *,
        db_path: str | Path = "~/.local-rag/lancedb",
        embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
        perf_config: dict[str, Any] | None = None,
        registry_path: str | Path | None = None,
    ) -> None:
        self.kb = kb
        self.db_path = Path(db_path).expanduser().resolve()
        self.store: VectorStoreProtocol = create_vector_store(
            backend=kb.vector_backend or "lancedb",
            db_path=str(self.db_path),
            kb_name=kb.name,
            dimensions=kb.dimensions,
            model=kb.embed_model,
        )
        # P0-4 / P1-8: performance config from registry.yaml `performance:`
        if perf_config is None:
            if registry_path is None:
                registry_path = SCRIPTS_DIR.parent / "registry.yaml"
            perf_config = load_perf_config(registry_path)
        self.perf_config = perf_config
        self._embed_batch_size = int(perf_config.get("embed_batch_size", 64))
        self._embed_fn = embed_fn or (
            lambda texts: embed_texts(
                texts, model=kb.embed_model, batch_size=self._embed_batch_size
            )
        )
    # ------------------------------------------------------------------
    # 文件发现与增量检测
    # ------------------------------------------------------------------

    def _scan_disk_files(self) -> list[Path]:
        """扫描知识库目录，返回所有匹配 patterns / 大小 / 数量限制的文件（不含增量过滤）。"""
        root = Path(self.kb.root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"知识库根目录不存在: {root}")

        files: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.stat().st_size > self.kb.max_file_bytes:
                continue
            if not any(fnmatch.fnmatch(path.name.lower(), p.lower()) for p in self.kb.patterns):
                continue
            if self.kb.path_filter and self.kb.path_filter not in str(path):
                continue
            files.append(path)
        return files[: self.kb.max_files]

    def _get_indexed_mtimes(self) -> dict[str, int]:
        """获取已索引文件的 mtime_ns（用于增量检测）。"""
        try:
            df = self.store.table.to_pandas()
            if df.empty:
                return {}
            result = df.groupby("path")["mtime_ns"].max().to_dict()
            return {str(k): int(v) for k, v in result.items()}
        except Exception as exc:
            # 表尚未创建/为空 → 视为无已索引文件，属正常首跑
            print(f"[rag_indexer] 读取已索引 mtime 失败（首跑可忽略）: {exc}", file=sys.stderr)
            return {}

    def _discover_files(self, *, force: bool = False) -> tuple[list[Path], int, int]:
        """发现需要索引的文件：增量（mtime_ns 对比）或全量（force=True）。

        Returns:
            (files_to_index, files_discovered, files_skipped_unchanged)
        """
        disk_files = self._scan_disk_files()
        indexed_mtimes = {} if force else self._get_indexed_mtimes()
        to_index: list[Path] = []
        skipped = 0
        for f in disk_files:
            mtime = f.stat().st_mtime_ns
            if not force and str(f) in indexed_mtimes and indexed_mtimes[str(f)] == mtime:
                skipped += 1
                continue
            to_index.append(f)
        return to_index, len(disk_files), skipped

    # ------------------------------------------------------------------
    # index() 各阶段（单一职责，可独立测试 / 单独失败隔离）
    # ------------------------------------------------------------------

    def _warmup_if_needed(self) -> None:
        """按 perf_config.warmup_on_index 发一次极小 embedding 请求触发 GPU/算子预热（失败不阻断）。"""
        if not self.perf_config.get("warmup_on_index", True):
            return
        try:
            embed_texts(["warmup"], model=self.kb.embed_model, batch_size=1, timeout=WARMUP_TIMEOUT)
        except Exception as exc:
            print(f"[warmup] 预热失败（不阻断索引）: {exc}", file=sys.stderr)

    def _parse_document(self, file_path: Path) -> Any:
        """调用 ingest 把文件解析为 Document。"""
        return ingest(file_path)

    def _chunk_document(self, doc: Any, file_path: Path) -> list[Any]:
        """按 doc_type 智能分块，并注入路径/标题/来源等文档级元数据。"""
        return chunk_document(
            doc.content,
            doc_type=doc.doc_type,
            doc_metadata={
                "path": str(file_path),
                "doc_type": doc.doc_type,
                "title": doc.title,
                "source": doc.path,
                **{k: v for k, v in doc.metadata.items() if isinstance(v, (str, int, float, bool))},
            },
        )

    def _embed_chunks(self, chunks: list[Any]) -> list[list[float]]:
        """批量生成 chunk 向量（batch_size 来自 perf_config）。"""
        texts = [c.text for c in chunks]
        return self._embed_fn(texts)

    def _upsert_chunks(
        self,
        chunks: list[Any],
        vectors: list[list[float]],
        doc: Any,
        file_path: Path,
        mtime_ns: int,
    ) -> int:
        """把 chunks + 向量组装成记录写入向量库，返回新增（upserted）条数。"""
        records = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            meta = chunk.metadata or {}
            records.append({
                "vector": vector,
                "chunk_id": chunk.chunk_id or f"{file_path}:{chunk.start_pos}-{chunk.end_pos}",
                "text": chunk.text,
                "path": str(file_path),
                "doc_type": doc.doc_type,
                "heading_path": meta.get("heading_path", []),
                "section_title": meta.get("section_title", ""),
                "start_line": meta.get("start_line", 0),
                "end_line": meta.get("end_line", 0),
                "mtime_ns": mtime_ns,
                "model": self.kb.embed_model,
                "metadata": {k: v for k, v in meta.items() if k not in ("heading_path", "section_title")},
            })
        result = self.store.upsert_chunks(records)
        return int(result["inserted"])

    def _upsert_pdf_page_images_if_needed(self, doc: Any, file_path: Path, mtime_ns: int) -> None:
        """PDF 文档：把每页渲染图经 vl-embedding-2b 入库（失败不阻断主流程）。"""
        if doc.doc_type != "pdf" or not doc.pages:
            return
        try:
            from rag_client import embed_images
            image_paths = [
                p.metadata.get("webp_path")
                for p in doc.pages
                if p.metadata.get("webp_path") and Path(p.metadata["webp_path"]).exists()
            ]
            if not image_paths:
                return
            img_vectors = embed_images(
                image_paths,
                batch_size=int(self.perf_config.get("image_batch_size", 64)),
            )
            img_records = []
            for page, vec in zip(doc.pages, img_vectors, strict=True):
                webp = page.metadata.get("webp_path", "")
                img_records.append({
                    "vector": vec,
                    "image_id": f"{file_path}:page_{page.number:03d}",
                    "path": str(file_path),
                    "page_num": page.number,
                    "description": page.content[:PDF_PAGE_DESC_TRUNCATE],
                    "doc_type": "pdf_page",
                    "mtime_ns": mtime_ns,
                    "model": DEFAULT_VL_IMAGE_MODEL,
                    "metadata": {"webp_path": webp, "text_extractor": page.metadata.get("text_extractor", "")},
                })
            self.store.upsert_images(img_records)
        except Exception as exc:
            print(f"[image-embed] PDF页面图向量失败 {file_path}: {exc}", file=sys.stderr)

    def _index_single_file(self, file_path: Path) -> dict[str, Any]:
        """索引单个文件的完整流水线：parse → chunk → embed → upsert → (pdf图)。

        返回 {path, chunks_embedded, chunks_upserted}；
        空分块文件返回全 0，视为成功（不抛错）。
        """
        doc = self._parse_document(file_path)
        chunks = self._chunk_document(doc, file_path)
        if not chunks:
            return {"path": str(file_path), "chunks_embedded": 0, "chunks_upserted": 0}
        vectors = self._embed_chunks(chunks)
        mtime_ns = file_path.stat().st_mtime_ns
        upserted = self._upsert_chunks(chunks, vectors, doc, file_path, mtime_ns)
        self._upsert_pdf_page_images_if_needed(doc, file_path, mtime_ns)
        return {
            "path": str(file_path),
            "chunks_embedded": len(chunks),
            "chunks_upserted": upserted,
        }

    def _prune_orphans(self, existing_paths: set[str]) -> int:
        """清理向量库中不在 existing_paths 的孤儿 chunks。"""
        return int(self.store.delete_orphans(existing_paths))

    def _build_result(
        self,
        *,
        discovered: int,
        skipped: int,
        indexed: int,
        failed: int,
        chunks: int,
        entities: int,
        pruned: int,
        start_time: float,
        errors: list[dict[str, str]],
    ) -> dict[str, Any]:
        """组装索引返回字典（统一字段结构，正常路径与 skip 路径共用）。"""
        return {
            "kb": self.kb.name,
            "root": self.kb.root,
            "files_discovered": discovered,
            "files_skipped_unchanged": skipped,
            "files_indexed": indexed,
            "files_failed": failed,
            "chunks_indexed": chunks,
            "entities_indexed": entities,
            "orphan_files_pruned": pruned,
            "total_chunks_in_store": self.store.count(),
            "elapsed_seconds": round(time.time() - start_time, 2),
            "errors": errors,
        }

    def _skip_result(
        self,
        *,
        discovered: int,
        skipped: int,
        start_time: float,
        reason: str = "",
    ) -> dict[str, Any]:
        """无可索引文件时的统一返回（字段结构与正常索引返回保持一致）。"""
        return self._build_result(
            discovered=discovered,
            skipped=skipped,
            indexed=0,
            failed=0,
            chunks=0,
            entities=0,
            pruned=0,
            start_time=start_time,
            errors=[],
        )

    # ------------------------------------------------------------------
    # 主流程（只做编排）
    # ------------------------------------------------------------------

    def index(
        self,
        *,
        force: bool = False,
        prune: bool = True,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        """构建或刷新索引：加锁 → 预热 → 发现文件 → 逐文件索引 → prune → optimize。"""
        start_time = time.time()
        with _IndexLock():
            self._warmup_if_needed()
            to_index, discovered, skipped = self._discover_files(force=force)
            if not to_index:
                return self._skip_result(discovered=discovered, skipped=skipped, start_time=start_time, reason="no files to index")
            files_result: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            total_upserted = 0
            for processed, file_path in enumerate(to_index, 1):
                if progress_callback:
                    progress_callback(processed, len(to_index), str(file_path))
                try:
                    fr = self._index_single_file(file_path)
                    files_result.append(fr)
                    total_upserted += fr.get("chunks_upserted", 0)
                except Exception as exc:
                    errors.append({"path": str(file_path), "error": str(exc)[:ERROR_MSG_TRUNCATE]})
            pruned = self._prune_orphans({str(f) for f in self._scan_disk_files()}) if (prune and not force) else 0
            try:
                self.store.optimize()
            except Exception as exc:
                print(f"[optimize] 失败（不阻断索引）: {exc}", file=sys.stderr)
            return self._build_result(discovered=discovered, skipped=skipped, indexed=len(files_result), failed=len(errors), chunks=total_upserted, entities=0, pruned=pruned, start_time=start_time, errors=errors[:MAX_REPORTED_ERRORS])

    def check_freshness(self) -> dict[str, Any]:
        """检查索引新鲜度：比较文件系统和索引中的 mtime。

        Returns:
            {
                "is_fresh": bool,
                "total_files": int,
                "indexed_files": int,
                "modified_files": int,      # 文件系统比索引新
                "new_files": int,           # 文件系统有但索引没有
                "deleted_files": int,       # 索引有但文件系统没有
                "stale_files": [path, ...], # 需要重新索引的文件（前20个）
                "last_index_time": str,     # 索引中最新的 mtime
            }
        """
        files = self._scan_disk_files()
        indexed = self.store.get_indexed_files()

        modified = []
        new = []
        deleted = []

        indexed_paths = set(indexed.keys())
        fs_paths = {str(f) for f in files}

        for f in files:
            path = str(f)
            mtime = f.stat().st_mtime_ns
            if path not in indexed:
                new.append(path)
            elif indexed[path] < mtime:
                modified.append(path)

        for path in indexed_paths:
            if path not in fs_paths:
                deleted.append(path)

        # 索引中最新的 mtime
        last_index_time = ""
        if indexed:
            max_mtime = max(indexed.values())
            last_index_time = datetime.fromtimestamp(max_mtime / 1e9).isoformat()

        stale = (modified + new)[:MAX_STALE_REPORTED]

        return {
            "is_fresh": len(modified) == 0 and len(new) == 0 and len(deleted) == 0,
            "total_files": len(files),
            "indexed_files": len(indexed),
            "modified_files": len(modified),
            "new_files": len(new),
            "deleted_files": len(deleted),
            "stale_files": stale,
            "last_index_time": last_index_time,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RAG 索引编排器")
    ap.add_argument("--registry", default=str(SCRIPTS_DIR.parent / "registry.yaml"),
                    help="知识库注册表路径")
    ap.add_argument("--kb", required=True, help="知识库名称")
    ap.add_argument("--db", default="~/.local-rag/lancedb", help="LanceDB 路径")
    sub = ap.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="建立/更新索引（默认）")
    p_index.add_argument("--force", action="store_true", help="强制全量重建")
    p_index.add_argument("--no-prune", action="store_true", help="不清理孤儿 chunks")

    p_fresh = sub.add_parser("freshness", help="检查索引新鲜度")
    p_stats = sub.add_parser("stats", help="查看索引统计")

    args = ap.parse_args()
    if args.command is None:
        args.command = "index"

    registry = load_registry(args.registry)
    if args.kb not in registry:
        print(f"ERROR: 知识库 {args.kb!r} 不存在。可用: {', '.join(registry)}", file=sys.stderr)
        sys.exit(1)

    kb = registry[args.kb]
    indexer = RAGIndexer(kb, db_path=args.db, registry_path=args.registry)

    if args.command == "freshness":
        result = indexer.check_freshness()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["is_fresh"]:
            print(f"\n⚠️  索引不新鲜：{result['modified_files']} 个文件已修改，"
                  f"{result['new_files']} 个新文件，{result['deleted_files']} 个已删除",
                  file=sys.stderr)
            print("运行 index 子命令更新索引", file=sys.stderr)
            sys.exit(2)
    elif args.command == "stats":
        print(json.dumps(indexer.store.stats(), ensure_ascii=False, indent=2))
    else:
        def progress(p, t, path):
            print(f"  [{p}/{t}] {Path(path).name}", file=sys.stderr)

        result = indexer.index(
            force=getattr(args, "force", False),
            prune=not getattr(args, "no_prune", False),
            progress_callback=progress,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
