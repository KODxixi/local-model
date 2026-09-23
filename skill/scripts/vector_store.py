"""LanceDB 向量存储：替代 SQLite 暴力搜索的 ANN 索引。

设计原则：
- 每个知识库一个 LanceDB table（按 kb name）
- ANN 搜索（IVF_PQ 或 HNSW），不是全表扫描
- 支持元数据过滤（path、doc_type、heading_path 等）
- 增量更新（按 path + mtime_ns 去重）
- 兼容旧 SQLite 索引（提供迁移工具）
- 列式存储 + ANN 索引（LanceDB + pyarrow）

Schema:
- vector: fixed_size_list<float>[dimensions]
- chunk_id: string
- text: string
- path: string
- doc_type: string
- heading_path: string (JSON array)
- section_title: string
- start_line: int32
- end_line: int32
- mtime_ns: int64
- model: string
- metadata: string (JSON)
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import lancedb
import pyarrow as pa

# ---------------------------------------------------------------------------
# 模块级常量（唯一来源；任何函数默认值/阈值都引用这里，禁止散落硬编码）
# ---------------------------------------------------------------------------

#: 默认文本 embedding 模型（Qwen3 Embedding 0.6B）及其输出维度；领域案例 视觉库独立管理。
DEFAULT_TEXT_EMBED_MODEL = "text-embedding-qwen3-embedding-0.6b"
DEFAULT_TEXT_EMBED_DIM = 1024

#: 默认图片 embedding 模型（vl-embedding-2b）及其输出维度
DEFAULT_IMAGE_EMBED_MODEL = "vl-embedding-2b"
DEFAULT_IMAGE_EMBED_DIM = 2048

#: 搜索默认返回条数
DEFAULT_SEARCH_TOP_K = 8
DEFAULT_IMAGE_SEARCH_TOP_K = 5

#: upsert 后总行数超过此阈值才触发索引构建/优化（避免小表付索引成本）
INDEX_UPSERT_TRIGGER_ROWS = 5000
#: 触发 ANN 索引的最小行数；更少时暴力扫描足够快，建索引无意义
INDEX_MIN_ROWS = 200
#: IVF 分区数 = clamp(total // INDEX_ROWS_PER_PARTITION, 1, INDEX_MAX_PARTITIONS)
INDEX_MAX_PARTITIONS = 256
INDEX_ROWS_PER_PARTITION = 500
#: 自上次构建后行数增长超过此比例才重建索引（1.5 = 增长 50%）
INDEX_REBUILD_GROWTH_RATIO = 1.5


# ---------------------------------------------------------------------------
# 抽象接口（P0: VectorStore 抽象层）
# ---------------------------------------------------------------------------

@runtime_checkable
class VectorStoreProtocol(Protocol):
    """向量存储抽象接口。任何后端（LanceDB/Qdrant/Chroma）实现此接口即可替换。

    结构化子类型（runtime_checkable）：实现方无需显式继承，只要具备同名方法
    即满足 isinstance 检查。新增后端 = 实现本协议 + ``create_vector_store``
    加一个分支 + registry.yaml 改 ``vector_store.type``。
    """

    def upsert_chunks(self, chunks: list[dict]) -> dict: ...
    def search(self, query_vector: list[float], top_k: int = 8, path_filter: str | None = None) -> list[dict]: ...
    def delete_by_path(self, path: str) -> int: ...
    def stats(self) -> dict: ...
    def delete_orphans(self, existing_paths: set[str]) -> int: ...
    def optimize(self) -> dict: ...
    def close(self) -> None: ...


@runtime_checkable
class ImageStoreProtocol(Protocol):
    """图片向量存储抽象接口（多模态后端实现）。

    文本后端可选择性实现；当前 LanceDBVectorStore 将文本表与图片表合并在
    同一类中。search_images / upsert_images 不在 VectorStoreProtocol 内，
    因为它们是图片存储（而非通用文本向量存储）的能力。
    """

    def upsert_images(self, images: list[dict]) -> dict: ...
    def search_images(
        self,
        query_vector: list[float],
        *,
        top_k: int = 5,
        path_filter: str | None = None,
    ) -> list[dict]: ...
    def delete_images_by_path(self, path: str) -> int: ...
    def image_stats(self) -> dict: ...


#: 向量存储工厂类型：按 backend 名称创建 VectorStoreProtocol 实例。
VectorStoreFactory = Callable[..., VectorStoreProtocol]


# ---------------------------------------------------------------------------
# SQL escaping (P0-5): LanceDB SQL uses single-quoted string literals.
# Paths / queries containing ' (e.g. O'Neil.md) must double them, otherwise
# the WHERE clause silently fails with a syntax error.
# ---------------------------------------------------------------------------

def _escape_sql(value: str) -> str:
    """Escape a string for safe embedding into a LanceDB SQL WHERE literal.

    Doubles single quotes (' -> ''). Does NOT escape %/_ because LanceDB's
    SQL does not universally support ESCAPE clauses; callers that build a
    LIKE pattern are responsible for wildcard semantics on their side.
    """
    if value is None:
        return ""
    return str(value).replace("'", "''")


def _safe_json_load(value: Any, default: Any) -> Any:
    """安全解析 JSON 字段：字符串则 json.loads，否则直接返回。

    LanceDB 中 heading_path/metadata 可能存储为原生 list/dict，
    也可能存储为 JSON 字符串（取决于写入路径）。统一处理避免类型不一致。
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return value


def _sql_like_escape(value: str) -> str:
    """Escape value for use inside a LIKE literal (% and _ are wildcards).

    We don't add an ESCAPE clause because LanceDB's SQL dialect may not
    support it; instead we neutralize wildcards so user input can't turn
    '%foo%' into an overly-broad match.
    """
    return _escape_sql(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _compact_table(table: Any) -> None:
    """压缩/优化 LanceDB 表碎片（best-effort，失败不抛）。

    LanceDB >=0.21 用 ``Table.optimize()`` 替代已废弃的 ``compact_files()``
    （compact + prune 旧版本 + 优化索引，是超集）。优先新 API；旧版本无
    ``optimize()`` 时回退到 ``compact_files()``。任何失败都只记 stderr——
    碎片压缩是性能优化，不影响正确性。
    """
    try:
        table.optimize()
    except AttributeError:
        # intentional: LanceDB <0.21 无 optimize()，回退到已废弃 compact_files
        try:
            table.compact_files()
        except Exception as exc:
            print(f"[vector_store] compact_files 回退失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
    except Exception as exc:
        print(f"[vector_store] optimize 失败: {type(exc).__name__}: {exc}",
              file=sys.stderr)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def make_schema(dimensions: int) -> pa.Schema:
    """构造文本向量表的 LanceDB/pyarrow schema。

    Args:
        dimensions: 向量维度（必须与 embedding 模型输出一致）。

    Returns:
        pyarrow Schema，含 vector/chunk_id/text/path/... 等字段。
    """
    return pa.schema([
        pa.field("vector", pa.list_(pa.float32(), dimensions)),
        pa.field("chunk_id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("path", pa.string()),
        pa.field("doc_type", pa.string()),
        pa.field("heading_path", pa.string()),  # JSON array
        pa.field("section_title", pa.string()),
        pa.field("start_line", pa.int32()),
        pa.field("end_line", pa.int32()),
        pa.field("mtime_ns", pa.int64()),
        pa.field("model", pa.string()),
        pa.field("metadata", pa.string()),  # JSON
    ])


def make_image_schema(dimensions: int = DEFAULT_IMAGE_EMBED_DIM) -> pa.Schema:
    """图片向量表 schema（2048维，vl-embedding-2b）。"""
    return pa.schema([
        pa.field("vector", pa.list_(pa.float32(), dimensions)),
        pa.field("image_id", pa.string()),
        pa.field("path", pa.string()),       # 源文件路径
        pa.field("page_num", pa.int32()),     # PDF页码，图片文件为0
        pa.field("description", pa.string()), # VLM生成的描述
        pa.field("doc_type", pa.string()),
        pa.field("mtime_ns", pa.int64()),
        pa.field("model", pa.string()),
        pa.field("metadata", pa.string()),    # JSON
    ])


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class LanceDBVectorStore:
    """LanceDB 向量存储，每个知识库一个 table。

    结构化实现 ``VectorStoreProtocol``（无需显式继承）。图片表操作
    （upsert_images / search_images 等）是 LanceDB 特有的多模态能力，
    保留在本类中，不在通用 Protocol 内。
    """

    def __init__(
        self,
        db_path: str | Path,
        kb_name: str,
        dimensions: int = DEFAULT_TEXT_EMBED_DIM,
        model: str = DEFAULT_TEXT_EMBED_MODEL,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.kb_name = kb_name
        self.dimensions = dimensions
        self.model = model
        self.db_path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.db_path))
        # P0-2: track whether ANN index has been built and row count at build
        # time so we only rebuild when rows have grown >50%.
        self._index_built = False
        self._index_rows_at_build = 0
        self._image_index_built = False
        self._image_index_rows_at_build = 0
        self._table = self._get_or_create_table()

    def _get_or_create_table(self):
        table_name = f"kb_{self.kb_name}"
        try:
            table = self._db.open_table(table_name)
        except Exception:
            # intentional: open_table 失败 = 表不存在（首跑），直接按 schema 新建
            schema = make_schema(self.dimensions)
            table = self._db.create_table(table_name, schema=schema)
            return table

        # P1-5: validate vector dimension against config. Opening an existing
        # table with mismatched dimensions silently corrupts every search.
        try:
            vec_field = table.schema.field("vector")
            existing_dim = int(vec_field.type.list_size)
            if existing_dim != self.dimensions:
                raise ValueError(
                    f"表 {table_name} 实际维度 {existing_dim} 与配置 {self.dimensions} 不符，"
                    f"请在新数据库中从原始资料重建，再切换配置；--force 不会改变旧表 schema。"
                )
        except ValueError:
            raise
        except Exception as exc:
            # If schema introspection fails for any reason, warn but do not
            # block startup (defensive; dimension check is best-effort).
            print(
                f"[vector_store] 无法校验表 {table_name} 维度: {exc}",
                file=sys.stderr,
            )
        return table

    @property
    def table(self):
        """底层 LanceDB Table 对象（只读暴露，供调试/高级查询）。"""
        return self._table

    def close(self) -> None:
        """释放资源（满足 ``VectorStoreProtocol.close`` 契约）。

        LanceDB 是文件式嵌入式存储，连接无需显式关闭；此方法留作接口契约，
        未来切换到需要连接管理的后端（Qdrant/Chroma 客户端）时在此实现。
        """

    def count(self) -> int:
        """返回表中 chunk 总行数。"""
        return int(self._table.count_rows())

    # -----------------------------------------------------------------------
    # 写入
    # -----------------------------------------------------------------------

    def upsert_chunks(self, chunks: list[dict[str, Any]]) -> dict[str, int]:
        """插入或更新 chunks。

        崩溃安全顺序（P0-6）：先 add 新行，再删除同 path 下不在新批次中的旧行；
        中途崩溃只会留重复，不会丢数据。

        Args:
            chunks: 每个 dict 含 vector, chunk_id, text, path, doc_type,
                    heading_path, section_title, start_line, end_line,
                    mtime_ns, metadata

        Returns:
            {"inserted": n, "deleted_old": n, "total": count}

        Raises:
            ValueError: 输入向量维度与表 schema 不一致（降级链切换模型）。
        """
        if not chunks:
            return {"inserted": 0, "deleted_old": 0, "total": self.count()}

        self._check_chunk_dimensions(chunks)

        rows = [self._normalize_chunk_row(c) for c in chunks]
        paths = list({r["path"] for r in rows})

        # 先用临时 ID 写入新批次，避免同一 chunk_id 的旧行和新行无法区分。
        # 若在中途崩溃，临时行仍包含完整新数据；下次 upsert 会将其一并清理。
        token = uuid4().hex
        staged_rows = []
        for i, row in enumerate(rows):
            staged_rows.append({**row, "chunk_id": f"__upsert_{token}_{i}"})

        paths_sql = ", ".join(f"'{_escape_sql(path)}'" for path in paths)
        staged_sql = ", ".join(f"'{row['chunk_id']}'" for row in staged_rows)
        self._table.add(staged_rows)
        self._table.delete(f"path IN ({paths_sql}) AND chunk_id NOT IN ({staged_sql})")
        self._table.add(rows)
        self._table.delete(f"chunk_id IN ({staged_sql})")
        deleted = len(paths)

        total = self.count()
        if total > INDEX_UPSERT_TRIGGER_ROWS:
            self._ensure_index()

        return {
            "inserted": len(rows),
            "deleted_old": deleted,
            "total": total,
        }

    def _check_chunk_dimensions(self, chunks: list[dict[str, Any]]) -> None:
        """校验输入向量维度与表 schema 一致（P1-2：防降级链维度不一致）。

        Raises:
            ValueError: 维度不匹配。
        """
        actual_dim = len(chunks[0]["vector"])
        if actual_dim != self.dimensions:
            raise ValueError(
                f"向量维度不匹配: 表 'kb_{self.kb_name}' 定义 {self.dimensions} 维, "
                f"实际输入 {actual_dim} 维。"
                f"可能是模型或注册表切换导致文本向量空间不一致。"
                f"请检查 registry.yaml 的 embed_model/dimensions 配置，"
                f"或使用 embed_texts(include_dimensions=True) 检测维度变化。"
            )

    def _normalize_chunk_row(self, c: dict[str, Any]) -> dict[str, Any]:
        """把外部 chunk dict 规范化为 LanceDB 表行（补默认值/类型/JSON 序列化）。"""
        return {
            "vector": [float(v) for v in c["vector"]],
            "chunk_id": c.get("chunk_id", ""),
            "text": c.get("text", ""),
            "path": c.get("path", ""),
            "doc_type": c.get("doc_type", "text"),
            "heading_path": json.dumps(c.get("heading_path", []), ensure_ascii=False),
            "section_title": c.get("section_title", ""),
            "start_line": int(c.get("start_line", 0)),
            "end_line": int(c.get("end_line", 0)),
            "mtime_ns": int(c.get("mtime_ns", 0)),
            "model": c.get("model", self.model),
            "metadata": json.dumps(c.get("metadata", {}), ensure_ascii=False),
        }

    def delete_by_path(self, path: str) -> int:
        """删除指定路径的所有 chunks。

        Returns:
            成功删除返回 1；表不存在/删除失败返回 0（错误已记 stderr）。
        """
        try:
            self._table.delete(f"path = '{_escape_sql(path)}'")
            return 1
        except Exception as exc:
            print(f"[vector_store] delete_by_path 失败 {path}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0

    def delete_orphans(self, existing_paths: set[str]) -> int:
        """删除不在 existing_paths 中的孤儿 chunks。

        Returns:
            删除的孤儿文件数；失败返回 0（错误已记 stderr）。
        """
        # LanceDB 不支持 NOT IN 直接删除，需要先查询再删
        try:
            all_paths = self._table.to_pandas()["path"].unique()
            orphans = [p for p in all_paths if p not in existing_paths]
            for p in orphans:
                self._table.delete(f"path = '{_escape_sql(p)}'")
            return len(orphans)
        except Exception as exc:
            print(f"[vector_store] delete_orphans 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0

    # -----------------------------------------------------------------------
    # 搜索
    # -----------------------------------------------------------------------

    def search(
        self,
        query_vector: list[float],
        *,
        top_k: int = DEFAULT_SEARCH_TOP_K,
        path_filter: str | None = None,
        doc_type_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """ANN 向量搜索。

        向量已由 llama-swap 做 L2 归一化（--embd-normalize 2），
        所以 LanceDB 默认的 L2 距离与 cosine 等价：L2 = 2(1 - cosine)。

        Args:
            query_vector: 查询向量
            top_k: 返回数量
            path_filter: 路径包含过滤
            doc_type_filter: 文档类型过滤

        Returns:
            结果列表
        """
        query = self._table.search(query_vector)

        if path_filter:
            query = query.where(
                f"path LIKE '%{_sql_like_escape(path_filter)}%'", prefilter=True
            )

        if doc_type_filter:
            query = query.where(
                f"doc_type = '{_escape_sql(doc_type_filter)}'", prefilter=True
            )

        results = query.limit(top_k).to_list()

        # 格式化输出
        output = []
        for r in results:
            output.append({
                "chunk_id": r.get("chunk_id", ""),
                "text": r.get("text", ""),
                "path": r.get("path", ""),
                "doc_type": r.get("doc_type", ""),
                "heading_path": json.loads(r.get("heading_path", "[]")),
                "section_title": r.get("section_title", ""),
                "start_line": r.get("start_line", 0),
                "end_line": r.get("end_line", 0),
                # LanceDB 返回 _distance（L2 距离，越低越相似），转换为相似度（越高越相似）
                # 便于 hybrid 合并时按 score 降序排列，与 keyword score（0-1）量级一致
                "score": round(1.0 / (1.0 + float(r.get("_distance", 0))), 6),
                "metadata": json.loads(r.get("metadata", "{}")),
            })
        return output

    def keyword_search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_SEARCH_TOP_K,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """关键词搜索：优先 BM25 FTS（中文 ngram），失败降级 LIKE 包含匹配。

        BM25 使用 LanceDB 全文索引（base_tokenizer=ngram, ngram 2-4），
        对中文短语友好；FTS 索引不存在或查询失败时自动降级到 Pandas LIKE。

        查询失败时返回空列表（错误已记 stderr）。
        """
        # 优先 BM25 FTS
        try:
            results = self._keyword_search_bm25(query, top_k=top_k, path_filter=path_filter)
            if results:
                return results
        except Exception as exc:
            print(f"[vector_store] BM25 失败，降级 LIKE: {type(exc).__name__}: {exc}",
                  file=sys.stderr)

        # 降级：LIKE 包含匹配
        return self._keyword_search_like(query, top_k=top_k, path_filter=path_filter)

    def _keyword_search_bm25(
        self,
        query: str,
        *,
        top_k: int,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        """BM25 全文检索（LanceDB FTS，中文 ngram 分词）。

        流程：path_filter 用 where(prefilter=True) 做服务端预过滤 → BM25 排名 → top_k 截断。
        预过滤在 FTS 排名前执行，避免目标候选全局排名靠后时被取数上限截断。
        contains(path, '...') 是字面包含匹配，支持 _、%、单引号和 Windows 路径。
        """
        self._ensure_fts_index()
        search = self._table.search(query, query_type="fts")
        if path_filter:
            search = search.where(
                f"contains(path, '{_escape_sql(path_filter)}')",
                prefilter=True,
            )
        rs = search.limit(top_k).to_list()

        output = []
        for r in rs:
            output.append({
                "chunk_id": r.get("chunk_id", ""),
                "text": r.get("text", ""),
                "path": r.get("path", ""),
                "doc_type": r.get("doc_type", ""),
                "heading_path": _safe_json_load(r.get("heading_path"), []),
                "score": 0.0,  # RRF 用排名，score 不参与融合
                "metadata": _safe_json_load(r.get("metadata"), {}),
            })
        return output

    def _keyword_search_like(
        self,
        query: str,
        *,
        top_k: int,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        """LIKE 包含匹配（Pandas 全表扫描，BM25 不可用时的降级方案）。"""
        try:
            df = self._table.to_pandas()
            mask = df["text"].str.contains(query, regex=False, na=False)
            if path_filter:
                mask = mask & df["path"].str.contains(path_filter, regex=False, na=False)
            results = df[mask].head(top_k).to_dict("records")
        except Exception as exc:
            print(f"[vector_store] keyword_search(LIKE) 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            results = []

        output = []
        for r in results:
            output.append({
                "chunk_id": r.get("chunk_id", ""),
                "text": r.get("text", ""),
                "path": r.get("path", ""),
                "doc_type": r.get("doc_type", ""),
                "heading_path": json.loads(r.get("heading_path", "[]")),
                "score": 0.0,
                "metadata": json.loads(r.get("metadata", "{}")),
            })
        return output

    def _ensure_fts_index(self) -> None:
        """懒加载创建 BM25 全文索引（中文 ngram 分词）。

        索引已存在时跳过；创建失败时打印警告，后续查询会降级到 LIKE。
        """
        if getattr(self, "_fts_checked", False):
            return
        self._fts_checked = True

        # 检测 FTS 索引：LanceDB list_indices 返回 IndexConfig，
        # FTS 索引的 index_type 包含 "FTS"，且 columns 包含 "text"
        try:
            indexes = self._table.list_indices()
            for idx in indexes:
                idx_type = str(getattr(idx, "index_type", "")).upper()
                columns = getattr(idx, "columns", []) or []
                if "FTS" in idx_type and "text" in columns:
                    return  # FTS 索引已存在
        except Exception:
            pass  # list_indices 失败时尝试创建

        try:
            self._table.create_fts_index(
                "text",
                base_tokenizer="ngram",
                ngram_min_length=2,
                ngram_max_length=4,
                replace=True,
            )
            print(f"[vector_store] FTS 索引已创建 (ngram 2-4): kb_{self.kb_name}",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[vector_store] FTS 索引创建失败（将用 LIKE 降级）: {exc}",
                  file=sys.stderr)

    def get_indexed_files(self) -> dict[str, int]:
        """获取索引中所有文件及其最新 mtime_ns。

        Returns:
            {path: max_mtime_ns} 字典；表为空/读取失败返回 {}（错误已记 stderr）。
        """
        try:
            # 用 LanceDB 的 SQL 查询获取每个 path 的最大 mtime
            df = self._table.to_pandas()
            if df.empty:
                return {}
            result = df.groupby("path")["mtime_ns"].max().to_dict()
            return {str(k): int(v) for k, v in result.items()}
        except Exception as exc:
            print(f"[vector_store] get_indexed_files 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return {}

    # -----------------------------------------------------------------------
    # 索引管理
    # -----------------------------------------------------------------------

    def _ensure_index(self) -> None:
        """创建或优化 ANN 索引（P0-2 修复：之前只 compact_files，从不建索引）。

        Uses IVF_HNSW_SQ on the vector column with cosine metric. Adaptive
        partition count = max(1, min(256, total_rows // 500)).

        Build is only triggered when:
          - no index has been built this process, OR
          - row count has grown by >50% since last build (keeps recall fresh
            without paying the build cost on every incremental upsert).
        """
        total = self.count()
        # Need enough rows for IVF partitions to make sense; below this the
        # brute-force scan is fast enough anyway.
        if total < INDEX_MIN_ROWS:
            return

        rebuild = (not self._index_built) or (
            self._index_rows_at_build > 0
            and total > self._index_rows_at_build * INDEX_REBUILD_GROWTH_RATIO
        )
        if not rebuild:
            return

        num_partitions = max(
            1, min(INDEX_MAX_PARTITIONS, total // INDEX_ROWS_PER_PARTITION)
        )
        try:
            self._table.create_index(
                metric="cosine",
                index_type="IVF_HNSW_SQ",
                vector_column_name="vector",
                num_partitions=num_partitions,
            )
            self._index_built = True
            self._index_rows_at_build = total
        except Exception as exc:
            print(
                f"[vector_store] create_index 失败（表 kb_{self.kb_name}）: {exc}",
                file=sys.stderr,
            )

        # 建完索引后合并碎片（新 API optimize 自动 compact + prune + 索引优化）。
        _compact_table(self._table)

    def optimize(self) -> dict[str, Any]:
        """优化表（碎片压缩 + 旧版本清理 + 索引优化，P0 升级至 ``Table.optimize``）。"""
        before = self.count()
        try:
            self._table.optimize()
        except AttributeError:
            # intentional: LanceDB <0.21 无 optimize()，回退到已废弃的 compact_files
            self._table.compact_files()
        except Exception as exc:
            return {"optimized": False, "error": str(exc), "rows": before}
        return {"optimized": True, "rows": self.count(), "before": before}

    # -----------------------------------------------------------------------
    # 统计
    # -----------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """返回表统计信息。"""
        try:
            df = self._table.to_pandas()
            return {
                "kb": self.kb_name,
                "total_chunks": len(df),
                "unique_files": df["path"].nunique() if len(df) else 0,
                "doc_types": df["doc_type"].value_counts().to_dict() if len(df) else {},
                "dimensions": self.dimensions,
                "model": self.model,
                "db_path": str(self.db_path),
            }
        except Exception as exc:
            return {"kb": self.kb_name, "error": str(exc)}

    # -----------------------------------------------------------------------
    # 图片向量表（2048维，vl-embedding-2b）
    # -----------------------------------------------------------------------

    def _get_image_table(self):
        """获取或创建图片向量表。"""
        table_name = f"kb_{self.kb_name}_images"
        try:
            return self._db.open_table(table_name)
        except Exception:
            # intentional: open_table 失败 = 表不存在（首跑），按图片 schema 新建
            schema = make_image_schema(DEFAULT_IMAGE_EMBED_DIM)
            return self._db.create_table(table_name, schema=schema)

    def upsert_images(self, images: list[dict[str, Any]]) -> dict[str, int]:
        """插入或更新图片向量。

        Args:
            images: 每个 dict 含 vector, image_id, path, page_num,
                    description, doc_type, mtime_ns, metadata

        Returns:
            {"inserted": n, "updated": 0}
        """
        if not images:
            return {"inserted": 0, "updated": 0}
        table = self._get_image_table()

        # P1-4: clean up old records for the same paths before adding new
        # ones. Previously this only added, so re-indexing the same PDF doubled
        # the image vectors every run.
        paths_to_clean = list({img.get("path", "") for img in images if img.get("path")})
        for p in paths_to_clean:
            try:
                table.delete(f"path = '{_escape_sql(p)}'")
            except Exception as exc:
                # 表为空/不存在时的首跑清理可忽略，记录便于排障
                print(f"[vector_store] 清理旧图片记录失败 {p}: {exc}", file=sys.stderr)

        rows = []
        for img in images:
            rows.append({
                "vector": img["vector"],
                "image_id": img["image_id"],
                "path": img["path"],
                "page_num": img.get("page_num", 0),
                "description": img.get("description", ""),
                "doc_type": img.get("doc_type", "image"),
                "mtime_ns": img.get("mtime_ns", 0),
                "model": img.get("model", DEFAULT_IMAGE_EMBED_MODEL),
                "metadata": json.dumps(img.get("metadata", {}), ensure_ascii=False),
            })
        table.add(rows)

        # P0-2: build ANN index on image table too.
        total = table.count_rows()
        if total >= INDEX_MIN_ROWS:
            rebuild = (not self._image_index_built) or (
                self._image_index_rows_at_build > 0
                and total > self._image_index_rows_at_build * INDEX_REBUILD_GROWTH_RATIO
            )
            if rebuild:
                num_partitions = max(
                    1, min(INDEX_MAX_PARTITIONS, total // INDEX_ROWS_PER_PARTITION)
                )
                try:
                    table.create_index(
                        metric="cosine",
                        index_type="IVF_HNSW_SQ",
                        vector_column_name="vector",
                        num_partitions=num_partitions,
                    )
                    self._image_index_built = True
                    self._image_index_rows_at_build = total
                except Exception as exc:
                    print(
                        f"[vector_store] 图片表 create_index 失败: {exc}",
                        file=sys.stderr,
                    )
                _compact_table(table)

        return {"inserted": len(rows), "updated": 0}

    def search_images(
        self,
        query_vector: list[float],
        *,
        top_k: int = DEFAULT_IMAGE_SEARCH_TOP_K,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """以图搜图：向量相似度检索。

        Returns:
            命中图片记录列表；表为空/查询失败返回 []（错误已记 stderr）。
        """
        table = self._get_image_table()
        try:
            results = table.search(query_vector).limit(top_k)
            if path_filter:
                results = results.where(
                    f"path LIKE '%{_sql_like_escape(path_filter)}%'"
                )
            df = results.to_pandas()
            return [
                {
                    "image_id": row["image_id"],
                    "path": row["path"],
                    "page_num": int(row["page_num"]),
                    "description": row["description"],
                    "doc_type": row["doc_type"],
                    "score": round(1.0 / (1.0 + float(row.get("_distance", 0))), 6),
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                }
                for _, row in df.iterrows()
            ]
        except Exception as exc:
            print(f"[vector_store] search_images 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return []

    def delete_images_by_path(self, path: str) -> int:
        """删除某个文件的所有图片向量。

        Returns:
            实际删除条数；失败返回 0（错误已记 stderr）。
        """
        table = self._get_image_table()
        try:
            before = table.count_rows()
            table.delete(f"path = '{_escape_sql(path)}'")
            after = table.count_rows()
            return before - after
        except Exception as exc:
            print(f"[vector_store] delete_images_by_path 失败 {path}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 0

    def image_stats(self) -> dict[str, Any]:
        """图片表统计。

        Returns:
            统计字典；表为空/读取失败返回零值（错误已记 stderr）。
        """
        try:
            table = self._get_image_table()
            df = table.to_pandas()
            return {
                "total_images": len(df),
                "unique_files": df["path"].nunique() if len(df) else 0,
                "dimensions": DEFAULT_IMAGE_EMBED_DIM,
                "model": DEFAULT_IMAGE_EMBED_MODEL,
            }
        except Exception as exc:
            print(f"[vector_store] image_stats 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return {"total_images": 0, "unique_files": 0}


# ---------------------------------------------------------------------------
# 向后兼容别名 + 工厂函数（P0: VectorStore 抽象层）
# ---------------------------------------------------------------------------

#: 旧名字别名，保持既有调用方不 break。
#: 新代码应通过 :func:`create_vector_store` 或类型注解 ``VectorStoreProtocol`` 使用。
VectorStore = LanceDBVectorStore


def create_vector_store(
    backend: str = "lancedb",
    db_path: str = "~/.local-rag/lancedb",
    kb_name: str = "",
    dimensions: int = DEFAULT_TEXT_EMBED_DIM,
    model: str = "",
    **kwargs,
) -> VectorStoreProtocol:
    """按 backend 类型创建向量存储实例。

    Args:
        backend: 后端类型，取自 registry.yaml 的 ``vector_store.type``。
            当前仅支持 ``"lancedb"``；新增后端只需在此加分支并实现
            :class:`VectorStoreProtocol`。
        db_path: 数据库路径。
        kb_name: 知识库名。
        dimensions: 向量维度。
        model: embedding 模型名（空串则用 LanceDBVectorStore 默认模型）。
        **kwargs: 透传给具体后端构造函数。

    Returns:
        满足 :class:`VectorStoreProtocol` 的存储实例。

    Raises:
        ValueError: 不支持的 backend。
    """
    if backend == "lancedb":
        return LanceDBVectorStore(
            db_path=db_path,
            kb_name=kb_name,
            dimensions=dimensions,
            model=model or DEFAULT_TEXT_EMBED_MODEL,
            **kwargs,
        )
    raise ValueError(f"Unsupported backend: {backend}. Supported: lancedb")


# ---------------------------------------------------------------------------
# 从旧 SQLite 索引迁移
# ---------------------------------------------------------------------------

def migrate_from_sqlite(
    sqlite_path: str | Path,
    lancedb_path: str | Path,
    kb_name: str,
    dimensions: int = DEFAULT_TEXT_EMBED_DIM,
    model: str = DEFAULT_TEXT_EMBED_MODEL,
) -> dict[str, Any]:
    """从旧的 SQLite 索引迁移到 LanceDB。

    旧 SQLite schema（indexer.py）：
    chunks(root, path, mtime_ns, start_line, end_line, text, embedding BLOB, dimensions, model)
    """
    import sqlite3
    import struct

    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        return {"error": f"SQLite file not found: {sqlite_path}"}

    store = VectorStore(lancedb_path, kb_name, dimensions=dimensions, model=model)

    conn = sqlite3.connect(str(sqlite_path))
    rows = conn.execute(
        "SELECT path, mtime_ns, start_line, end_line, text, embedding FROM chunks WHERE model = ?",
        (model,),
    ).fetchall()
    conn.close()

    chunks = []
    for path, mtime_ns, start_line, end_line, text, blob in rows:
        vector = list(struct.unpack(f"<{len(blob) // 4}f", blob))
        if len(vector) != dimensions:
            continue
        chunks.append({
            "vector": vector,
            "chunk_id": f"{path}:{start_line}-{end_line}",
            "text": text,
            "path": path,
            "doc_type": "text",
            "heading_path": [],
            "section_title": "",
            "start_line": start_line,
            "end_line": end_line,
            "mtime_ns": mtime_ns,
            "model": model,
            "metadata": {"migrated_from": "sqlite", "sqlite_path": str(sqlite_path)},
        })

    result: dict[str, Any] = store.upsert_chunks(chunks)
    result["migrated_from"] = str(sqlite_path)
    result["kb"] = kb_name
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="LanceDB 向量存储管理")
    sub = ap.add_subparsers(dest="command")

    # stats
    p_stats = sub.add_parser("stats", help="查看表统计")
    p_stats.add_argument("--db", default="~/.local-rag/lancedb")
    p_stats.add_argument("--kb", required=True)

    # migrate
    p_mig = sub.add_parser("migrate", help="从 SQLite 迁移")
    p_mig.add_argument("--sqlite", required=True)
    p_mig.add_argument("--db", default="~/.local-rag/lancedb")
    p_mig.add_argument("--kb", required=True)
    p_mig.add_argument("--dimensions", type=int, default=DEFAULT_TEXT_EMBED_DIM)

    # optimize
    p_opt = sub.add_parser("optimize", help="优化表")
    p_opt.add_argument("--db", default="~/.local-rag/lancedb")
    p_opt.add_argument("--kb", required=True)

    args = ap.parse_args()

    if args.command == "stats":
        store = VectorStore(args.db, args.kb)
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    elif args.command == "migrate":
        result = migrate_from_sqlite(args.sqlite, args.db, args.kb, args.dimensions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "optimize":
        store = VectorStore(args.db, args.kb)
        print(json.dumps(store.optimize(), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
