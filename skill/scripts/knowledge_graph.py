"""知识图谱：实体提取 + 关系链接 + 相关文档检索。

核心思路：
1. 从文档中提取实体（人名、地名、项目名、技术名、概念等）
2. 存储实体及其出现的文档（倒排索引）
3. 检索时，通过实体找到相关文档（"找提到 X 的文档"）
4. 支持实体关系查询（"X 和 Y 同时出现在哪些文档"）

这是对向量检索的补充——向量检索找"语义相似"，知识图谱找"实体关联"。
两者结合可以显著提升召回率和精准度。
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lancedb
import pyarrow as pa

# P0-5: reuse SQL escaping helper from vector_store to keep single source.
from vector_store import _escape_sql  # noqa: E402

try:
    from rag_enhance import _llm_chat
    _HAS_LLM = True
except ImportError:
    _HAS_LLM = False


# ---------------------------------------------------------------------------
# 默认参数常量（避免散落的魔法数字）
# ---------------------------------------------------------------------------
DEFAULT_LLM_EXTRACT_MAX_CHARS = 3000  # 送入 LLM 实体提取的最大字符数
REGEX_MAX_ENTITIES = 10                # 规则提取最多返回实体数
ENTITY_CONTEXT_CHARS = 50             # 实体上下文窗口（前后各 N 字）
RELATED_DOC_SCAN = 100                # 找相关实体时扫描的文档数上限
RELATED_PER_DOC_LIMIT = 50            # 单文档内取实体记录上限
SEARCH_LIMIT_MULTIPLIER = 3           # find_docs 初取 top_k*N 条再聚合
LIST_LIMIT_MULTIPLIER = 5             # list_entities 初取 top_k*N 条再聚合

#: LLM 实体提取生成参数
LLM_EXTRACT_TEMPERATURE = 0.1   # 低温度：抽取任务要求稳定
LLM_EXTRACT_MAX_TOKENS = 1024
#: 规则提取默认 top_k
DEFAULT_RELATED_TOP_K = 10


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Entity:
    """实体。"""
    name: str
    entity_type: str  # person / place / project / tech / concept / org / other
    description: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class EntityOccurrence:
    """实体在文档中的出现。"""
    entity_name: str
    entity_type: str
    path: str
    context: str  # 实体出现的上下文（前后各50字）
    count: int = 1  # 在该文档中出现次数


# ---------------------------------------------------------------------------
# 实体提取
# ---------------------------------------------------------------------------

def extract_entities_llm(
    content: str,
    *,
    title: str = "",
    max_chars: int = DEFAULT_LLM_EXTRACT_MAX_CHARS,
) -> list[Entity]:
    """用本地 LLM 从文档中提取实体。

    Args:
        content: 文档内容
        title: 文档标题
        max_chars: 送入 LLM 的最大字符数

    Returns:
        Entity 列表
    """
    if not _HAS_LLM:
        return []

    truncated = content[:max_chars]
    if len(content) > max_chars:
        truncated += "\n...(已截断)"

    system_prompt = """你是一个实体提取专家。从给定的文档中提取重要实体。

实体类型：
- person: 人名
- place: 地名/位置
- project: 项目/产品名
- tech: 技术/工具/框架
- concept: 概念/理论/方法
- org: 组织/公司/机构
- other: 其他重要实体

输出格式（严格JSON数组，不要其他文字）：
[
  {"name": "实体名", "type": "类型", "description": "一句话描述", "aliases": ["别名1", "别名2"]}
]

规则：
- 只提取文档中明确提到的实体，不要推测
- 每个实体 name 用文档中最常见的称呼
- description 基于文档内容，不要用外部知识
- aliases 只列文档中出现的其他称呼
- 最多提取15个实体，优先提取反复出现或在标题中出现的"""

    user_prompt = f"文档标题：{title or '(无)'}\n\n文档内容：\n{truncated}"

    try:
        response = _llm_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=LLM_EXTRACT_TEMPERATURE,
            max_tokens=LLM_EXTRACT_MAX_TOKENS,
        )
        response = re.sub(r"^```json\s*", "", response)
        response = re.sub(r"\s*```$", "", response)
        data = json.loads(response)
        entities = []
        for item in data:
            entities.append(Entity(
                name=item.get("name", ""),
                entity_type=item.get("type", "other"),
                description=item.get("description", ""),
                aliases=item.get("aliases", []),
            ))
        return [e for e in entities if e.name]
    except Exception as exc:
        # LLM 输出非法 JSON / 调用失败：返回空，调用方应回退规则提取
        print(f"[knowledge_graph] LLM 实体提取失败，回退规则: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return []


def extract_entities_regex(content: str) -> list[Entity]:
    """用规则提取实体（LLM 不可用时的 fallback）。

    简单策略：提取大写开头的连续词、引号中的内容等。
    精度不高，但保证有基本能力。

    Args:
        content: 文档全文。

    Returns:
        识别出的 Entity 列表，最多 REGEX_MAX_ENTITIES 个。
    """
    entities = []
    seen = set()

    # 提取中文专有名词模式（2-6个字，大写开头或特定后缀）
    # 这是一个非常简化的实现，实际效果有限
    patterns = [
        (r'"([^"]{2,20})"', "concept"),  # 引号中的内容
        (r'「([^」]{2,20})」', "concept"),
        (r'《([^》]{2,20})》', "project"),  # 书名号
    ]

    for pattern, etype in patterns:
        for match in re.finditer(pattern, content):
            name = match.group(1)
            if name not in seen and len(name) >= 2:
                seen.add(name)
                entities.append(Entity(name=name, entity_type=etype))

    return entities[:REGEX_MAX_ENTITIES]


# ---------------------------------------------------------------------------
# 知识图谱存储
# ---------------------------------------------------------------------------

class KnowledgeGraph:
    """知识图谱：基于 LanceDB 的实体存储与检索。"""

    def __init__(self, db_path: str = "~/.local-rag/lancedb", kb_name: str = "default") -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.kb_name = kb_name
        self.table_name = f"kg_{kb_name}"
        self._db: Any = None  # 延迟初始化
        self._table: Any = None  # 延迟初始化

    def _connect(self) -> None:
        if self._db is None:
            self._db = lancedb.connect(self.db_path)
        if self._table is None:
            try:
                self._table = self._db.open_table(self.table_name)
            except Exception:
                # 表不存在 → 首次创建，属正常控制流
                self._table = self._db.create_table(self.table_name, schema=self._schema())
        assert self._table is not None  # mypy: _connect() 后 _table 一定非 None

    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("entity_name", pa.string()),
            pa.field("entity_type", pa.string()),
            pa.field("description", pa.string()),
            pa.field("path", pa.string()),
            pa.field("context", pa.string()),
            pa.field("count", pa.int32()),
            pa.field("mtime_ns", pa.int64()),
        ])

    def add_entities(
        self,
        entities: list[Entity],
        path: str,
        content: str,
        mtime_ns: int = 0,
    ) -> int:
        """为一个文档添加实体。

        Args:
            entities: 提取的实体列表
            path: 文档路径
            content: 文档内容（用于找上下文）
            mtime_ns: 文件修改时间

        Returns:
            添加的实体数量
        """
        self._connect()

        # 先删除该文档的旧实体
        with contextlib.suppress(Exception):
            self._table.delete(f"path = '{_escape_sql(path)}'")

        records = []
        for entity in entities:
            # 找实体在内容中的上下文
            context = ""
            idx = content.find(entity.name)
            if idx >= 0:
                start = max(0, idx - ENTITY_CONTEXT_CHARS)
                end = min(len(content), idx + len(entity.name) + ENTITY_CONTEXT_CHARS)
                context = content[start:end]

            # 统计出现次数
            count = content.count(entity.name)

            records.append({
                "entity_name": entity.name,
                "entity_type": entity.entity_type,
                "description": entity.description,
                "path": path,
                "context": context,
                "count": count,
                "mtime_ns": mtime_ns,
            })

        if records:
            self._table.add(records)
        return len(records)

    def find_docs_by_entity(self, entity_name: str, *, top_k: int = DEFAULT_RELATED_TOP_K) -> list[dict[str, Any]]:
        """找到提到某个实体的所有文档。

        Args:
            entity_name: 实体名（支持模糊匹配）
            top_k: 返回数量

        Returns:
            文档列表，按实体出现次数排序
        """
        self._connect()
        try:
            results = (
                self._table.search()
                .where(f"entity_name LIKE '%{_escape_sql(entity_name)}%'", prefilter=True)
                .limit(top_k * SEARCH_LIMIT_MULTIPLIER)
                .to_list()
            )
        except Exception as exc:
            print(f"[knowledge_graph] find_docs_by_entity 查询失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return []

        # 按 path 聚合
        by_path: dict[str, dict[str, Any]] = {}
        for r in results:
            path = r.get("path", "")
            if path not in by_path:
                by_path[path] = {
                    "path": path,
                    "entity_count": 0,
                    "entities": [],
                    "contexts": [],
                }
            by_path[path]["entity_count"] += r.get("count", 1)
            by_path[path]["entities"].append(r.get("entity_name", ""))
            if r.get("context"):
                by_path[path]["contexts"].append(r["context"])

        # 按出现次数排序
        sorted_docs = sorted(by_path.values(), key=lambda x: x["entity_count"], reverse=True)
        return sorted_docs[:top_k]

    def find_related_entities(self, entity_name: str, *, top_k: int = DEFAULT_RELATED_TOP_K) -> list[dict[str, Any]]:
        """找到与某个实体共同出现的其他实体。

        Args:
            entity_name: 实体名
            top_k: 返回数量

        Returns:
            相关实体列表，按共现次数排序
        """
        self._connect()

        # 先找到提到该实体的所有文档
        docs = self.find_docs_by_entity(entity_name, top_k=RELATED_DOC_SCAN)
        doc_paths = [d["path"] for d in docs]

        if not doc_paths:
            return []

        # 在这些文档中找其他实体
        related: dict[str, dict[str, Any]] = {}
        for path in doc_paths:
            try:
                path_filter = _escape_sql(path)
                entities_in_doc = (
                    self._table.search()
                    .where(f"path = '{path_filter}'", prefilter=True)
                    .limit(RELATED_PER_DOC_LIMIT)
                    .to_list()
                )
            except Exception as exc:
                print(f"[knowledge_graph] find_related_entities 单文档查询失败 {path}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                continue

            for e in entities_in_doc:
                name = e.get("entity_name", "")
                if name == entity_name or entity_name in name:
                    continue
                if name not in related:
                    related[name] = {
                        "entity_name": name,
                        "entity_type": e.get("entity_type", ""),
                        "cooccurrence_count": 0,
                        "docs": [],
                    }
                related[name]["cooccurrence_count"] += 1
                if path not in related[name]["docs"]:
                    related[name]["docs"].append(path)

        sorted_entities = sorted(
            related.values(), key=lambda x: x["cooccurrence_count"], reverse=True
        )
        return sorted_entities[:top_k]

    def list_entities(self, *, entity_type: str | None = None, top_k: int = 50) -> list[dict[str, Any]]:
        """列出所有实体（按出现频率排序）。

        Args:
            entity_type: 按类型过滤
            top_k: 返回数量

        Returns:
            实体列表
        """
        self._connect()
        try:
            if entity_type:
                results = (
                    self._table.search()
                    .where(f"entity_type = '{_escape_sql(entity_type)}'", prefilter=True)
                    .limit(top_k * LIST_LIMIT_MULTIPLIER)
                    .to_list()
                )
            else:
                results = self._table.search().limit(top_k * LIST_LIMIT_MULTIPLIER).to_list()
        except Exception as exc:
            print(f"[knowledge_graph] list_entities 查询失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return []

        # 按实体名聚合
        by_name: dict[str, dict[str, Any]] = {}
        for r in results:
            name = r.get("entity_name", "")
            if not name:
                continue
            if name not in by_name:
                by_name[name] = {
                    "entity_name": name,
                    "entity_type": r.get("entity_type", ""),
                    "description": r.get("description", ""),
                    "total_occurrences": 0,
                    "doc_count": 0,
                }
            by_name[name]["total_occurrences"] += r.get("count", 1)
            by_name[name]["doc_count"] += 1

        sorted_entities = sorted(
            by_name.values(), key=lambda x: x["total_occurrences"], reverse=True
        )
        return sorted_entities[:top_k]

    def stats(self) -> dict[str, Any]:
        """知识图谱统计。"""
        self._connect()
        try:
            df = self._table.to_pandas()
            if df.empty:
                return {"kb": self.kb_name, "total_occurrences": 0, "unique_entities": 0, "unique_docs": 0}
            return {
                "kb": self.kb_name,
                "total_occurrences": len(df),
                "unique_entities": df["entity_name"].nunique(),
                "unique_docs": df["path"].nunique(),
                "entity_types": df["entity_type"].value_counts().to_dict(),
            }
        except Exception as exc:
            print(f"[knowledge_graph] stats 失败（表为空或损坏）: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return {"kb": self.kb_name, "total_occurrences": 0, "unique_entities": 0, "unique_docs": 0}

    def delete_by_path(self, path: str) -> int:
        """删除某个文档的所有实体。"""
        self._connect()
        try:
            before = int(self._table.count_rows())
            self._table.delete(f"path = '{_escape_sql(path)}'")
            after = int(self._table.count_rows())
            return before - after
        except Exception as exc:
            print(f"[knowledge_graph] delete_by_path 失败 {path}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0

    def delete_orphans(self, existing_paths: set[str]) -> int:
        """删除不在 existing_paths 中的所有实体（P1-3 修复）。

        之前知识图谱从不清理孤儿，已删除文件的实体记录永久残留。
        """
        self._connect()
        try:
            all_paths = self._table.to_pandas()["path"].unique()
            orphans = [p for p in all_paths if p not in existing_paths]
            for p in orphans:
                self._table.delete(f"path = '{_escape_sql(p)}'")
            return len(orphans)
        except Exception as exc:
            print(f"[knowledge_graph] delete_orphans 失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 0

    # ------------------------------------------------------------------
    # 可视化：构建图谱数据 + 生成 HTML
    # ------------------------------------------------------------------
    def build_graph_data(self, *, top_k: int = 100) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """提取实体和共现关系，用于可视化。

        Args:
            top_k: 取出现频率最高的前 N 个实体

        Returns:
            (entities, relations)
            - entities: [{entity_name, entity_type, total_occurrences, doc_count, description, docs}]
            - relations: [{source, target, cooccurrence_count, docs}]
        """
        self._connect()
        try:
            df = self._table.to_pandas()
        except Exception as exc:
            print(f"[knowledge_graph] build_graph_data 读取失败: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return [], []

        if df.empty:
            return [], []

        # 按实体聚合
        by_name: dict[str, dict[str, Any]] = {}
        for _, row in df.iterrows():
            name = row.get("entity_name", "")
            if not name:
                continue
            if name not in by_name:
                by_name[name] = {
                    "entity_name": name,
                    "entity_type": row.get("entity_type", "other"),
                    "description": row.get("description", ""),
                    "total_occurrences": 0,
                    "doc_count": 0,
                    "docs": set(),
                }
            by_name[name]["total_occurrences"] += int(row.get("count", 1))
            by_name[name]["doc_count"] += 1
            by_name[name]["docs"].add(row.get("path", ""))

        # 取 top_k 实体
        sorted_entities = sorted(
            by_name.values(), key=lambda x: x["total_occurrences"], reverse=True
        )[:top_k]
        top_names = {e["entity_name"] for e in sorted_entities}

        # 构建共现关系：同一文档中出现的实体对
        doc_entities: dict[str, set[str]] = {}
        for _, row in df.iterrows():
            name = row.get("entity_name", "")
            path = row.get("path", "")
            if name in top_names and path:
                if path not in doc_entities:
                    doc_entities[path] = set()
                doc_entities[path].add(name)

        relations: dict[tuple[str, str], dict[str, Any]] = {}
        for path, entities in doc_entities.items():
            ent_list = sorted(entities)
            for i in range(len(ent_list)):
                for j in range(i + 1, len(ent_list)):
                    key = (ent_list[i], ent_list[j])
                    if key not in relations:
                        relations[key] = {
                            "source": ent_list[i],
                            "target": ent_list[j],
                            "cooccurrence_count": 0,
                            "docs": [],
                        }
                    relations[key]["cooccurrence_count"] += 1
                    relations[key]["docs"].append(path)

        # 转换 docs set 为 list
        entities_out = []
        for e in sorted_entities:
            e_copy = dict(e)
            e_copy["docs"] = sorted(e_copy["docs"])
            entities_out.append(e_copy)

        relations_out = sorted(
            relations.values(), key=lambda x: x["cooccurrence_count"], reverse=True
        )

        return entities_out, relations_out

    def visualize(self, output_path: str | Path, *, top_k: int = 100) -> Path:
        """生成知识图谱交互式 HTML。

        Args:
            output_path: 输出 HTML 路径
            top_k: 取出现频率最高的前 N 个实体

        Returns:
            输出文件路径
        """
        from visualize import render_knowledge_graph

        entities, relations = self.build_graph_data(top_k=top_k)
        return render_knowledge_graph(
            entities, relations, output_path, kb_name=self.kb_name
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="知识图谱：实体提取与检索")
    ap.add_argument("--db", default="~/.local-rag/lancedb", help="LanceDB 路径")
    ap.add_argument("--kb", default=None, help="知识库名称（必填）")
    sub = ap.add_subparsers(dest="command")

    p_extract = sub.add_parser("extract", help="从文件提取实体")
    p_extract.add_argument("file", help="文件路径")
    p_extract.add_argument("--no-llm", action="store_true", help="只用规则提取（不调用 LLM）")

    p_find = sub.add_parser("find", help="找提到某实体的文档")
    p_find.add_argument("entity", help="实体名")
    p_find.add_argument("--top-k", type=int, default=DEFAULT_RELATED_TOP_K)

    p_related = sub.add_parser("related", help="找与某实体共现的其他实体")
    p_related.add_argument("entity", help="实体名")
    p_related.add_argument("--top-k", type=int, default=DEFAULT_RELATED_TOP_K)

    p_list = sub.add_parser("list", help="列出所有实体")
    p_list.add_argument("--type", default=None, help="按类型过滤")
    p_list.add_argument("--top-k", type=int, default=50)

    p_stats = sub.add_parser("stats", help="知识图谱统计")

    args = ap.parse_args()
    if not args.kb:
        ap.error("--kb 必填：知识库名称，决定图谱表名 kg_{kb}")

    kg = KnowledgeGraph(db_path=args.db, kb_name=args.kb)

    if args.command == "extract":
        content = Path(args.file).read_text(encoding="utf-8", errors="replace")
        if args.no_llm:
            entities = extract_entities_regex(content)
        else:
            entities = extract_entities_llm(content, title=Path(args.file).stem)
        print(json.dumps([
            {"name": e.name, "type": e.entity_type, "description": e.description, "aliases": e.aliases}
            for e in entities
        ], ensure_ascii=False, indent=2))

    elif args.command == "find":
        results = kg.find_docs_by_entity(args.entity, top_k=args.top_k)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "related":
        results = kg.find_related_entities(args.entity, top_k=args.top_k)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "list":
        results = kg.list_entities(entity_type=args.type, top_k=args.top_k)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.command == "stats":
        print(json.dumps(kg.stats(), ensure_ascii=False, indent=2))

    else:
        ap.print_help()
