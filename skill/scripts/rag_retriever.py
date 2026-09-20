"""RAG 检索编排器：混合召回 → rerank → 上下文格式化。

这是 agent 调用的核心入口。agent 只需要调用 retrieve(kb, query)，
内部自动完成：语义检索 → 关键词检索 → 合并去重 → rerank 精排 → 格式化。

输出格式针对 agent 优化：
- 每个结果含：text（完整片段）、path（来源路径）、score（相关性）、
  heading_path（标题路径）、section_title（章节标题）、doc_type
- 按相关性排序，默认 top_k=8
- 支持元数据过滤（path、doc_type）
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from rag_client import DEFAULT_TEXT_RERANK_MODEL, embed_texts, rerank_texts
from rag_indexer import KBConfig, load_perf_config, load_registry
from vector_store import VectorStoreProtocol, create_vector_store

try:
    from rag_enhance import rewrite_query
    _HAS_ENHANCE = True
except ImportError:
    _HAS_ENHANCE = False


# ---------------------------------------------------------------------------
# P3 创新突破：MMR / Parent-Child / Query Routing 辅助
# ---------------------------------------------------------------------------

# 文本 token 化：ASCII 词/数字（长度>=2）+ 中日韩单字。
# 用于 MMR 的 chunk 间冗余度度量（store.search 不返回 vector 列，
# 且禁止改 vector_store.py，故用确定性文本相似度，零额外 embedding 调用）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[一-鿿]")
# 事实型信号：数字、日期、百分比、连续大写开头的专有名词
_FACT_RE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月|\d+\s*[%％]|\b\d{2,}\b"
    r"|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+"
)
# 语义型/开放式疑问词
_QUESTION_RE = re.compile(
    r"什么是|如何|为什么|怎么|怎样|哪些|解释|介绍|区别|原理|定义|谈谈|分析"
)
# P4 智能权重：引号 / 专有名词/数字 事实信号（复用 _FACT_RE）
_QUOTE_RE = re.compile(r"[\"'“”‘’「」『』]")
# 中文单字计数（智能权重的"词/字"长度度量）
_CJK_RE = re.compile(r"[一-鿿]")
# P4 多轮上下文消歧：代词表
_PRONOUNS = ("它", "他", "她", "这个", "那个", "这些", "那些", "其", "该")
# 上下文名词短语切分标点
_CTX_SPLIT_RE = re.compile(r"[，。！？；：、,.!?;:\s]+")

# ---------------------------------------------------------------------------
# 检索默认参数常量（唯一来源，CLI / 便捷函数保持行为一致）
# ---------------------------------------------------------------------------
DEFAULT_TOP_K = 8                  # 默认返回结果数
DEFAULT_RERANK_RECALL_SIZE = 24    # rerank 候选数（registry performance.rerank_recall_size 的兜底）
DEFAULT_MMR_LAMBDA = 0.5           # MMR 相关-多样性平衡系数
DEFAULT_PARENT_EXPAND_CHARS = 500  # Parent-Child 上下文扩展字符数（前/后各 N）
MAX_MULTI_QUERIES = 3              # 多查询检索最多子查询数
MAX_KEYWORD_TERMS = 5              # 关键词召回最多切分词数
DEDUP_KEY_CHARS = 100              # 去重键使用 text 前 N 字符
PARENT_ANCHOR_CHARS = 120         # Parent-Child 定位锚点使用 chunk 前 N 字符

# Qwen3-Embedding 查询指令前缀（仅查询侧，文档侧不变，不重建向量）
# 官方格式：Instruct: <task instruction>\nQuery: <user query>
# 可回退：RAGRetriever(use_query_instruction=False) 或 retrieve(use_query_instruction=False)
QWEN_QUERY_INSTRUCTION = "Instruct: Retrieve relevant passages that answer the question.\nQuery: "


def _dedup_key(r: dict[str, Any]) -> str:
    """稳定去重键：优先 chunk_id，缺失时用 path + 完整内容 sha256。

    P0 修复（codex 审查）：原实现用 path+text[:80]，同路径、前80字相同
    后文不同的两个片段会被误合并。完整内容哈希避免此问题。
    """
    cid = r.get("chunk_id")
    if cid:
        return str(cid)
    text = r.get("text", "") or ""
    path = r.get("path", "") or ""
    return f"{path}#{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _token_set(text: str) -> set[str]:
    """把文本切成 token 集合（小写）。"""
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: str, b: str) -> float:
    """两段文本的 Jaccard 相似度 [0,1]，度量冗余度。"""
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    if inter == 0:
        return 0.0
    return inter / len(sa | sb)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度 [0,1]（假设向量已近似归一化；未归一化也可用）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    sim = dot / (na * nb)
    return max(0.0, min(1.0, sim))


def _trace_candidates(
    candidates: list[dict[str, Any]], *, with_rrf: bool = False
) -> list[dict[str, Any]]:
    """把候选列表转为 trace 友好的精简格式（去掉 vector 等大字段）。"""
    out = []
    for i, c in enumerate(candidates, 1):
        item = {
            "rank": i,
            "chunk_id": c.get("chunk_id", ""),
            "text": c.get("text", "")[:200],
            "path": c.get("path", ""),
            "score": round(float(c.get("score", 0)), 6),
            "source": c.get("_source", "semantic"),
        }
        if with_rrf:
            item["rrf_score"] = round(float(c.get("_sort_score", 0)), 6)
        if c.get("metadata"):
            meta = c["metadata"]
            if isinstance(meta, dict) and "rrf_score" in meta:
                item["rrf_score"] = round(float(meta["rrf_score"]), 6)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 检索结果
# ---------------------------------------------------------------------------

@dataclass
class RetrievalResult:
    """单个检索结果。"""
    text: str
    path: str
    score: float
    doc_type: str = "text"
    heading_path: list[str] = field(default_factory=list)
    section_title: str = ""
    chunk_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    ranked_by: str = "rerank"  # rerank / embedding / keyword
    explain: dict[str, Any] = field(default_factory=dict)  # 思维链：为什么命中这个片段

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "path": self.path,
            "score": self.score,
            "doc_type": self.doc_type,
            "heading_path": self.heading_path,
            "section_title": self.section_title,
            "chunk_id": self.chunk_id,
            "metadata": self.metadata,
            "ranked_by": self.ranked_by,
            "explain": self.explain,
        }

    def format_for_agent(self, index: int) -> str:
        """格式化为 agent 友好的文本块。"""
        title_path = " > ".join(self.heading_path) if self.heading_path else self.section_title
        header = f"[{index}] {title_path or Path(self.path).name} (score={self.score:.4f}, {self.ranked_by})"
        source = f"    source: {self.path}"
        content = "\n".join(f"    {line}" for line in self.text.splitlines())
        return f"{header}\n{source}\n{content}"


# ---------------------------------------------------------------------------
# 检索器
# ---------------------------------------------------------------------------

class RAGRetriever:
    """RAG 检索编排器。"""

    def __init__(
        self,
        kb: KBConfig,
        *,
        db_path: str | Path = "~/.local-rag/lancedb",
        rerank_model: str = DEFAULT_TEXT_RERANK_MODEL,
        perf_config: dict[str, Any] | None = None,
        registry_path: str | Path | None = None,
        use_query_instruction: bool = True,
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
        self.rerank_model = rerank_model
        self.use_query_instruction = use_query_instruction
        if perf_config is None:
            if registry_path is None:
                registry_path = SCRIPTS_DIR.parent / "registry.yaml"
            perf_config = load_perf_config(registry_path)
        self.perf_config = perf_config

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        mode: str = "hybrid",  # hybrid / semantic / keyword
        path_filter: str | None = None,
        doc_type_filter: str | None = None,
        use_rerank: bool = True,
        use_query_rewrite: bool = False,
        recall_size: int | None = None,
        auto_route: bool = False,
        smart_weights: bool = True,
        context: str = "",
        use_query_instruction: bool | None = None,  # None=用 self 默认, True/False=覆盖
        trace: bool = False,
        trace_output: str | None = None,
        # 已砍功能的参数（保留签名兼容，但实际不生效）
        use_mmr: bool = False,
        mmr_lambda: float = 0.5,
        parent_child: bool = False,
        parent_expand_chars: int = 200,
        expand_query: bool = False,
        num_expansions: int = 2,
    ) -> list[RetrievalResult]:
        """检索知识库。

        Args:
            query: 查询文本
            top_k: 返回结果数
            mode: 检索模式
                - hybrid: 语义 + 关键词混合召回（默认）
                - semantic: 仅语义检索
                - keyword: 仅关键词检索（不调用模型）
            path_filter: 路径包含过滤
            doc_type_filter: 文档类型过滤
            use_rerank: 是否使用 rerank 精排
            use_query_rewrite: 是否使用 LLM 查询改写（提升命中率，但增加延迟）
            recall_size: 召回阶段取前 N 条给 rerank。默认从 registry
                performance.rerank_recall_size 读取（默认 24）。
            use_mmr: P3 是否在 rerank 后做 MMR 多样性重排（避免相邻 chunk 扎堆）。
            mmr_lambda: P3 MMR 平衡系数 [0,1]，1=纯相关性，0=纯多样性。
            parent_child: P3 是否把命中 chunk 扩展为父文档上下文。
            parent_expand_chars: P3 父文档扩展的字符数（前/后各 N）。
            auto_route: P3 是否按查询类型自动选择检索模式（仅当 mode=hybrid 时生效）。
            smart_weights: P4 是否按查询特征自动分配 semantic/keyword 混合权重（默认开启，纯改进）。
            expand_query: P4 是否用对话端点做多查询扩展召回（默认关闭，延迟 2-3x）。
            num_expansions: P4 扩展子查询数量（不含原查询，默认 2）。
            context: P4 多轮对话上下文（非空时先做规则消歧再检索）。
            trace: 是否记录检索推理链（存 self._last_trace）。
            trace_output: 推理链 HTML 输出路径（非空时自动生成可视化文件）。

        Returns:
            RetrievalResult 列表，按（MMR 后）相关性/多样性排序
        """
        import time as _time
        _t_start = _time.time()
        self._last_trace: dict[str, Any] = {
            "query": query,
            "kb": self.kb.name,
            "mode": mode,
            "steps": [],
        }
        _trace = self._last_trace if trace else None
        if not query.strip():
            raise ValueError("query 不能为空")

        # 查询指令：None 时用 self 默认值（构造时设置），True/False 覆盖
        # 用 getattr 兼容未走 __init__ 的 mock 对象
        default_qinstr = getattr(self, "use_query_instruction", True)
        effective_qinstr = default_qinstr if use_query_instruction is None else use_query_instruction
        self._query_instruction_override = effective_qinstr
        routed_mode = None  # auto_route 已砍，保留变量兼容

        # P0-3: default recall_size from registry performance section.
        if recall_size is None:
            recall_size = int(self.perf_config.get("rerank_recall_size", DEFAULT_RERANK_RECALL_SIZE))


        # P4-3: 多轮上下文消歧（规则-based，零延迟）
        disambiguated = False
        active_query = query
        if context:
            disambiguated_q = self._disambiguate_query(query, context)
            if disambiguated_q != query:
                active_query = disambiguated_q
                disambiguated = True

        # 查询改写（可选）
        if use_query_rewrite and _HAS_ENHANCE and mode != "keyword":
            rewritten = rewrite_query(active_query)
            active_query = rewritten.rewritten or active_query
            # 如果有子查询，做多查询检索
            if rewritten.sub_queries:
                return self._multi_query_retrieve(
                    [active_query] + rewritten.sub_queries,
                    top_k=top_k, mode=mode,
                    path_filter=path_filter, doc_type_filter=doc_type_filter,
                    use_rerank=use_rerank, recall_size=recall_size,
                    smart_weights=smart_weights,
                )

        # P4-2: 查询扩展（对话端点默认 8080，默认关闭；失败降级为单查询）
        expanded_queries_meta: list[str] = []
        expand_queries = [active_query]
        if expand_query and mode != "keyword":
            expanded = self._expand_query(active_query, num_expansions)
            expand_queries = expanded
            if len(expanded) > 1:
                expanded_queries_meta = expanded

        # Trace: 查询处理
        if _trace is not None:
            _trace["query_processing"] = {
                "original": query,
                "active_query": active_query,
                "disambiguated": disambiguated,
                "rewritten": use_query_rewrite and _HAS_ENHANCE and mode != "keyword",
                "expanded_queries": expanded_queries_meta,
                "query_instruction": effective_qinstr,
                "routed_mode": routed_mode,
            }

        # 1. 召回（支持多查询：按 chunk_id 合并，保留最高分）
        merged: dict[str, dict[str, Any]] = {}

        def _sort_key(r: dict[str, Any]) -> float:
            """hybrid 模式用 _sort_score（加权），其他模式用 score。"""
            return float(r.get("_sort_score", r.get("score", 0)))

        def _merge_cand(r: dict[str, Any]) -> None:
            key = _dedup_key(r)
            old = merged.get(key)
            if old is None or _sort_key(r) > _sort_key(old):
                merged[key] = r

        for q in expand_queries:
            if mode == "hybrid":
                for r in self._hybrid_recall(
                    q, recall_size, path_filter, doc_type_filter, smart_weights
                ):
                    _merge_cand(r)
            elif mode == "semantic":
                for r in self._semantic_recall(
                    q, recall_size, path_filter, doc_type_filter
                ):
                    _merge_cand(r)
            else:  # keyword
                for r in self._keyword_recall(q, recall_size, path_filter):
                    r["_source"] = "keyword"
                    _merge_cand(r)

        candidates: list[dict[str, Any]] = list(merged.values())

        # Trace: 召回结果
        if _trace is not None:
            sem_cands = [c for c in candidates if c.get("_source") != "keyword"]
            kw_cands = [c for c in candidates if c.get("_source") == "keyword"]
            if mode in ("hybrid", "semantic"):
                _trace["semantic_recall"] = {
                    "candidates": _trace_candidates(sem_cands),
                    "count": len(sem_cands),
                }
            if mode in ("hybrid", "keyword"):
                _trace["keyword_recall"] = {
                    "candidates": _trace_candidates(kw_cands),
                    "count": len(kw_cands),
                }
            if mode == "hybrid":
                _trace["rrf_fusion"] = {
                    "candidates": _trace_candidates(candidates, with_rrf=True),
                    "count": len(candidates),
                    "rrf_k": 60,
                    "weights": {"semantic": 0.5, "keyword": 0.5},  # 实际权重在 _hybrid_recall 中
                }
            _trace["deduplication"] = {
                "before": sum(len(expand_queries) for _ in [0]),  # 粗略估计
                "after": len(candidates),
            }

        if not candidates:
            if _trace is not None:
                _trace["total_time_ms"] = (_time.time() - _t_start) * 1000
            return []

        # P0-3: 粗排截断 — hybrid 召回可能送回 2×recall_size 条（语义+关键词），
        # rerank 每篇 ~0.36s，送 100 条要 36s。按召回分数排序后截断到
        # recall_size（默认 24）再送 rerank，单次检索降到 ~9s。
        if use_rerank and mode != "keyword" and len(candidates) > recall_size:
            candidates = sorted(
                candidates,
                key=_sort_key,
                reverse=True,
            )[:recall_size]

        # 3. rerank 精排
        # P3-1: 开启 MMR 时给它一个更宽的候选池（2×top_k），
        # 否则 MMR 只能重排已经截断到 top_k 的结果，失去多样性意义。
        rerank_pool = top_k * 2 if use_mmr else top_k
        _rerank_start = _time.time()
        if use_rerank and mode != "keyword":
            results = self._rerank(query, candidates, rerank_pool)
        else:
            # 不 rerank，按召回分数排序（hybrid 用 RRF _sort_score）
            results = sorted(candidates, key=_sort_key, reverse=True)[:rerank_pool]
            for r in results:
                r["ranked_by"] = "embedding" if r.get("_source") != "keyword" else "keyword"
                # P0 修复：RRF 分存入 metadata（供 MMR 使用），不覆盖原始 score
                # 原始 score 对语义结果是向量相似度，对关键词结果是 0.0
                if "_sort_score" in r:
                    r.setdefault("metadata", {})["rrf_score"] = float(r["_sort_score"])

        # Trace: rerank
        if _trace is not None:
            _trace["rerank"] = {
                "input_count": len(candidates),
                "results": _trace_candidates(results),
                "time_ms": (_time.time() - _rerank_start) * 1000,
                "model": self.rerank_model,
                "used": use_rerank and mode != "keyword",
            }

        # 4. 格式化为 RetrievalResult
        result_objs: list[RetrievalResult] = [self._to_result(r) for r in results]

        # 5. P3-1: MMR 多样性重排（在 rerank 之后，避免相邻 chunk 扎堆）
        if use_mmr and result_objs:
            result_objs = self._mmr_reorder(
                result_objs, query_vector=None,
                lambda_mult=mmr_lambda, top_k=top_k,
            )
        elif not use_mmr:
            result_objs = result_objs[:top_k]

        # 6. P3-2: Parent-Child 上下文扩展
        if parent_child and result_objs:
            result_objs = self._expand_to_parent(result_objs, expand_chars=parent_expand_chars)

        # P3-3: 在 metadata 中标记实际生效的检索模式
        for r in result_objs:
            if routed_mode is not None:
                r.metadata["routed_mode"] = routed_mode
            # P4-2: 查询扩展标记
            if expanded_queries_meta:
                r.metadata["expanded"] = True
                r.metadata["expanded_queries"] = expanded_queries_meta
            # P4-3: 上下文消歧标记
            if disambiguated:
                r.metadata["original_query"] = query
                r.metadata["disambiguated_query"] = active_query

        # Trace: 最终结果 + 生成 HTML
        if _trace is not None:
            _trace["final_results"] = [
                {
                    "rank": i,
                    "text": r.text[:200],
                    "path": r.path,
                    "score": round(r.score, 6),
                    "ranked_by": r.ranked_by,
                    "chunk_id": r.chunk_id,
                }
                for i, r in enumerate(result_objs, 1)
            ]
            _trace["total_time_ms"] = (_time.time() - _t_start) * 1000
            if trace_output:
                try:
                    from visualize import render_retrieval_trace
                    render_retrieval_trace(_trace, trace_output)
                    _trace["trace_html"] = str(trace_output)
                except Exception as exc:  # noqa: BLE001
                    print(f"[rag_retriever] 推理链 HTML 生成失败: {exc}", file=sys.stderr)

        return result_objs

    # ------------------------------------------------------------------
    # P3-1: MMR 多样性重排
    # ------------------------------------------------------------------
    def _mmr_reorder(
        self,
        results: list[RetrievalResult],
        query_vector: list[float] | None = None,
        lambda_mult: float = 0.5,
        top_k: int = 8,
    ) -> list[RetrievalResult]:
        """MMR（Maximal Marginal Relevance）多样性重排。

        MMR = λ * Sim(query, doc) - (1-λ) * max(Sim(doc, selected))

        - 相关性项：直接复用 rerank 后的 result.score（无需再算 query-doc 余弦）。
        - 冗余度项：store.search 不返回 vector 列且禁止改 vector_store.py，
          故用文本 token Jaccard 度量 chunk 间相似度；若 metadata 中恰好带
          "vector"，则改用余弦相似度（零额外 embedding 调用）。

        Args:
            results: rerank 后的候选（按相关性已大致排好）
            query_vector: 查询向量（可选；当前流程不额外调 embedding，传 None）
            lambda_mult: 平衡系数，1=纯相关性（保持原序），0=纯多样性
            top_k: 最终选出的数量

        Returns:
            重排后的 top_k 列表
        """
        if not results:
            return []
        k = min(top_k, len(results))
        lam = max(0.0, min(1.0, lambda_mult))

        # 把 score 归一化到 [0,1]，便于与 Jaccard[0,1] 加权
        # P0 修复：hybrid 不 rerank 时，优先用 metadata.rrf_score（RRF 融合分），
        # 否则关键词结果 score=0 会被 MMR 排到最后，撤销 RRF 的融合排序
        scores = [
            float((r.metadata or {}).get("rrf_score", r.score))
            for r in results
        ]
        smin, smax = min(scores), max(scores)
        span = smax - smin

        def _relevance(i: int) -> float:
            if span <= 1e-9:
                return 0.5
            return (scores[i] - smin) / span

        # 预取每个 chunk 的向量（若有），否则用文本
        vecs: list[list[float] | None] = [
            r.metadata.get("vector") if isinstance(r.metadata, dict) else None
            for r in results
        ]

        def _sim(i: int, j: int) -> float:
            if vecs[i] is not None and vecs[j] is not None:
                return _cosine(vecs[i], vecs[j])
            return _jaccard(results[i].text, results[j].text)

        selected: list[int] = []
        pool = set(range(len(results)))

        for _ in range(k):
            best_idx = -1
            best_val = -math.inf
            for i in pool:
                # 与已选集合的最大冗余度
                redundancy = max(_sim(i, j) for j in selected) if selected else 0.0
                mmr_val = lam * _relevance(i) - (1.0 - lam) * redundancy
                if mmr_val > best_val:
                    best_val = mmr_val
                    best_idx = i
            if best_idx < 0:
                break
            selected.append(best_idx)
            pool.discard(best_idx)

        return [results[i] for i in selected]

    # ------------------------------------------------------------------
    # P3-2: Parent-Child 父文档上下文扩展
    # ------------------------------------------------------------------

    def _route_query(self, query: str) -> str:
        """根据查询类型启发式选择检索模式（无需 LLM）。

        - 含数字/日期/百分比/大写专有名词 → keyword（事实型）
        - 含 什么是/如何/为什么/解释 等疑问词 → semantic（开放型）
        - 两者兼有 → hybrid
        - 默认 → hybrid
        """
        has_fact = bool(_FACT_RE.search(query))
        has_question = bool(_QUESTION_RE.search(query))
        if has_fact and has_question:
            return "hybrid"
        if has_fact:
            return "keyword"
        if has_question:
            return "semantic"
        return "hybrid"

    # ------------------------------------------------------------------
    # P4-1: 混合检索智能权重
    # ------------------------------------------------------------------
    @staticmethod
    def _query_length_units(query: str) -> int:
        """查询长度度量：中文按字符数，英文按空格分词数，取较大值。"""
        cn = len(_CJK_RE.findall(query))
        en = len(query.split())
        return max(cn, en)

    @staticmethod
    def _calculate_hybrid_weights(query: str) -> tuple[float, float]:
        """根据查询特征自动分配 semantic / keyword 召回权重。

        规则（按优先级从高到低）：
        - 含数字/日期/百分比/大写专有名词/引号（事实型精确查找）→ keyword 0.8
        - 含疑问词（什么/如何/为什么/怎么…）→ semantic 0.6
        - 短查询（<5 词/字）→ keyword 0.6（倾向精确匹配）
        - 长查询/自然语言（>15 词/字）→ semantic 0.7
        - 默认 → 各 0.5

        Returns:
            (semantic_weight, keyword_weight)，二者之和恒为 1.0
        """
        has_fact = bool(_FACT_RE.search(query)) or bool(_QUOTE_RE.search(query))
        has_question = bool(_QUESTION_RE.search(query))
        length = RAGRetriever._query_length_units(query)

        if has_fact:
            return (0.2, 0.8)
        if has_question:
            return (0.6, 0.4)
        if length < 5:
            return (0.4, 0.6)
        if length > 15:
            return (0.7, 0.3)
        return (0.5, 0.5)

    # ------------------------------------------------------------------
    # P4-2: 查询扩展（Query Expansion）
    # ------------------------------------------------------------------
    def _expand_query(self, query: str, num_expansions: int = 2) -> list[str]:
        """用对话端点（默认 8080）生成同义改写子查询，提升召回率。

        失败（对话端点不可用 / rewrite_query 抛异常）时降级为仅原查询。
        返回 [原查询] + 最多 num_expansions 个子查询。
        """
        if not _HAS_ENHANCE:
            return [query]
        try:
            rewritten = rewrite_query(query)
        except Exception as exc:  # noqa: BLE001 - 任何 LLM 故障都降级
            print(f"[rag_retriever] 查询扩展失败，降级为原查询: {exc}", file=sys.stderr)
            return [query]
        subs: list[str] = list(getattr(rewritten, "sub_queries", None) or [])
        out: list[str] = [query]
        for sq in subs:
            if len(out) - 1 >= num_expansions:
                break
            if sq and sq.strip() and sq.strip() != query:
                out.append(sq.strip())
        return out

    # ------------------------------------------------------------------
    # P4-3: 多轮上下文检索（规则-based 消歧，零额外延迟）
    # ------------------------------------------------------------------
    @staticmethod
    def _disambiguate_query(query: str, context: str) -> str:
        """用上一轮对话上下文消歧当前查询。

        - context 为空 → 原样返回
        - 查询含代词（它/他/她/这个/那个/这些/那些/其/该）→ 把 context 中
          最后一个名词短语前置，让代词有明确指代
        - 查询很短（<3 字）且 context 非空 → 拼接 context 关键词 + query
        - 其余 → 原样返回
        """
        if not context or not context.strip():
            return query
        parts = [p.strip() for p in _CTX_SPLIT_RE.split(context) if len(p.strip()) > 1]
        last_phrase = parts[-1] if parts else context.strip()
        has_pronoun = any(p in query for p in _PRONOUNS)
        if has_pronoun:
            return f"{last_phrase} {query}".strip()
        if len(query) < 3:
            return f"{last_phrase} {query}".strip()
        return query

    def _hybrid_recall(
        self,
        query: str,
        recall_size: int,
        path_filter: str | None,
        doc_type_filter: str | None,
        smart_weights: bool = True,
    ) -> list[dict[str, Any]]:
        """语义 + 关键词 RRF 融合召回。

        P0 修复（codex 审查）：原实现关键词 score 固定 0.0，混合后按分数截断
        导致关键词独有命中被语义结果全部挤掉，调高关键词权重也无效（0×权重=0）。

        现改用 RRF（Reciprocal Rank Fusion）：
        - 语义结果按向量相似度排名，关键词结果按匹配顺序排名
        - 每个候选最终分数 = Σ 1/(k + rank)，k=60（标准 RRF 参数）
        - 权重只影响各来源的 RRF 贡献系数，不改变原始 score
        - 输出保留原始 score（语义=相似度，关键词=0.0），_sort_score 存 RRF 融合分
        """
        sw, kw = (0.5, 0.5)
        if smart_weights:
            sw, kw = self._calculate_hybrid_weights(query)
        weights_meta = {"semantic": sw, "keyword": kw}

        RRF_K = 60  # 标准 RRF 常数

        semantic_results = self._semantic_recall(
            query, recall_size, path_filter, doc_type_filter
        )
        for rank, r in enumerate(semantic_results, start=1):
            r["_source"] = "semantic"
            r["_rrf_semantic"] = 1.0 / (RRF_K + rank)
            r["_hybrid_weights"] = weights_meta

        keyword_results = self._keyword_recall(query, recall_size, path_filter)
        for rank, r in enumerate(keyword_results, start=1):
            r["_source"] = "keyword"
            r["_rrf_keyword"] = 1.0 / (RRF_K + rank)
            r["_hybrid_weights"] = weights_meta

        # RRF 融合：按稳定去重键合并，分数 = sw*rrf_semantic + kw*rrf_keyword
        # P0 修复：统一用 _dedup_key（chunk_id 缺失时 path+完整内容 sha256），
        # 原前80字截取会把"同路径+相同前缀+不同后文"的两个片段误合并
        merged: dict[str, dict[str, Any]] = {}
        for r in semantic_results + keyword_results:
            key = _dedup_key(r)
            if key in merged:
                existing = merged[key]
                existing["_rrf_semantic"] = existing.get("_rrf_semantic", 0.0) + r.get("_rrf_semantic", 0.0)
                existing["_rrf_keyword"] = existing.get("_rrf_keyword", 0.0) + r.get("_rrf_keyword", 0.0)
                # 同时命中两个来源时，标记来源为 hybrid
                if existing.get("_source") != r.get("_source"):
                    existing["_source"] = "hybrid"
            else:
                merged[key] = r

        # 计算最终 RRF 融合分数作为排序依据
        result_list = list(merged.values())
        for r in result_list:
            rrf_sem = r.get("_rrf_semantic", 0.0)
            rrf_kw = r.get("_rrf_keyword", 0.0)
            r["_sort_score"] = sw * rrf_sem + kw * rrf_kw
        return result_list

    def _semantic_recall(
        self,
        query: str,
        recall_size: int,
        path_filter: str | None,
        doc_type_filter: str | None,
    ) -> list[dict[str, Any]]:
        """语义召回：embed → ANN 搜索。

        查询指令前缀从 self 读取（retrieve() 设置 _query_instruction_override）。
        """
        use_qinstr = getattr(self, "_query_instruction_override", getattr(self, "use_query_instruction", True))
        embed_query = QWEN_QUERY_INSTRUCTION + query if use_qinstr else query
        query_vector = embed_texts([embed_query], model=self.kb.embed_model)[0]
        results = self.store.search(
            query_vector,
            top_k=recall_size,
            path_filter=path_filter,
            doc_type_filter=doc_type_filter,
        )
        for r in results:
            r["_source"] = "semantic"
        return results


    def _keyword_recall(
        self,
        query: str,
        recall_size: int,
        path_filter: str | None,
    ) -> list[dict[str, Any]]:
        """关键词召回：LIKE 搜索（未来可升级 FTS5）。"""
        # 简单实现：按空格分割查询词，每个词搜索，合并结果
        terms = [t for t in query.split() if len(t) >= 2]
        if not terms:
            terms = [query]

        all_results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for term in terms[:MAX_KEYWORD_TERMS]:  # 最多 N 个词
            results = self.store.keyword_search(term, top_k=recall_size, path_filter=path_filter)
            for r in results:
                cid = _dedup_key(r)  # P0 修复：稳定去重键，原前100字截取会误合并
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    all_results.append(r)
        return all_results

    def _deduplicate(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """去重：语义和关键词结果可能重叠。"""
        seen: set[str] = set()
        unique = []
        for c in candidates:
            key = _dedup_key(c)  # P0 修复：稳定去重键
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        return unique

    def _rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """rerank 精排。"""
        texts = [c.get("text", "") for c in candidates]
        try:
            ranked = rerank_texts(query, texts, model=self.rerank_model, top_k=min(top_k, len(candidates)))
            results = []
            for item in ranked:
                idx = item["index"]
                c = candidates[idx]
                c["score"] = item["score"]
                c["ranked_by"] = "rerank"
                results.append(c)
            return results
        except Exception as exc:
            # rerank 失败，降级为召回排序（hybrid 用 RRF _sort_score，避免关键词 score=0 被挤掉）
            print(f"[rag_retriever] rerank 失败，降级为召回排序: {exc}", file=sys.stderr)
            results = sorted(
                candidates,
                key=lambda x: float(x.get("_sort_score", x.get("score", 0))),
                reverse=True,
            )[:top_k]
            for r in results:
                r["ranked_by"] = "embedding" if r.get("_source") != "keyword" else "keyword"
                if "_sort_score" in r:
                    r.setdefault("metadata", {})["rrf_score"] = float(r["_sort_score"])
            return results

    def _to_result(self, r: dict[str, Any]) -> RetrievalResult:
        meta = dict(r.get("metadata", {}) or {})
        # P4-1: 把加权混合召回的权重写入 metadata（若候选带了标记）
        if "_hybrid_weights" in r:
            meta.setdefault("hybrid_weights", r["_hybrid_weights"])

        # 思维链：为什么命中这个片段
        explain = {
            "source": r.get("_source", "unknown"),  # semantic / keyword / hybrid
            "ranked_by": r.get("ranked_by", "embedding"),
            "semantic_score": r.get("_rrf_sem", None),
            "keyword_score": r.get("_rrf_keyword", None),
            "rrf_score": r.get("_sort_score", None),
            "rerank_score": float(r.get("score", 0)) if r.get("ranked_by") == "rerank" else None,
        }
        # 去掉 None 值
        explain = {k: v for k, v in explain.items() if v is not None}

        return RetrievalResult(
            text=r.get("text", ""),
            path=r.get("path", ""),
            score=float(r.get("score", 0)),
            doc_type=r.get("doc_type", "text"),
            heading_path=r.get("heading_path", []),
            section_title=r.get("section_title", ""),
            chunk_id=r.get("chunk_id", ""),
            metadata=meta,
            ranked_by=r.get("ranked_by", "embedding"),
            explain=explain,
        )

    def _init_cache(self) -> None:
        """初始化 SQLite 缓存表。"""
        self._cache_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._cache_db))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retrieval_cache (
                cache_key TEXT PRIMARY KEY,
                results_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                index_mtime REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _cache_key(
        self,
        query: str,
        *,
        top_k: int,
        mode: str,
        path_filter: str | None,
        doc_type_filter: str | None,
        use_rerank: bool,
    ) -> str:
        """生成缓存 key。"""
        raw = f"{self.kb.name}|{query}|{top_k}|{mode}|{path_filter}|{doc_type_filter}|{use_rerank}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _get_index_mtime(self) -> float:
        """获取索引最后修改时间（用于缓存失效）。"""
        try:
            stats = self.store.stats()
            return float(stats.get("last_index_time", 0))
        except Exception:
            return time.time()  # 出错时用当前时间，相当于缓存失效

    def _get_cached(
        self,
        cache_key: str,
    ) -> list[RetrievalResult] | None:
        """从缓存读取结果。"""
        try:
            conn = sqlite3.connect(str(self._cache_db))
            row = conn.execute(
                "SELECT results_json, created_at, index_mtime FROM retrieval_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            conn.close()
            if not row:
                return None
            results_json, created_at, index_mtime = row
            # 检查 TTL
            if time.time() - created_at > self._cache_ttl:
                return None
            # 检查索引是否更新过
            current_mtime = self._get_index_mtime()
            if current_mtime > index_mtime:
                return None
            # 反序列化
            data = json.loads(results_json)
            return [RetrievalResult.from_dict(r) for r in data]
        except Exception:
            return None

    def _set_cached(
        self,
        cache_key: str,
        results: list[RetrievalResult],
    ) -> None:
        """写入缓存。"""
        try:
            results_json = json.dumps([r.to_dict() for r in results], ensure_ascii=False)
            index_mtime = self._get_index_mtime()
            conn = sqlite3.connect(str(self._cache_db))
            conn.execute(
                "INSERT OR REPLACE INTO retrieval_cache (cache_key, results_json, created_at, index_mtime) VALUES (?, ?, ?, ?)",
                (cache_key, results_json, time.time(), index_mtime),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass  # 缓存失败不影响检索

    def format_results(self, results: list[RetrievalResult]) -> str:
        """将结果格式化为 agent 友好的文本。"""
        if not results:
            return "(无检索结果)"
        blocks = [r.format_for_agent(i + 1) for i, r in enumerate(results)]
        return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def retrieve(
    kb_name: str,
    query: str,
    *,
    registry_path: str | Path | None = None,
    db_path: str | Path = "~/.local-rag/lancedb",
    top_k: int = DEFAULT_TOP_K,
    mode: str = "hybrid",
    **kwargs,
) -> list[RetrievalResult]:
    """便捷函数：根据知识库名称直接检索。"""
    if registry_path is None:
        registry_path = SCRIPTS_DIR.parent / "registry.yaml"
    registry = load_registry(registry_path)
    if kb_name not in registry:
        raise ValueError(f"未知知识库 {kb_name!r}；可用: {', '.join(registry)}")
    retriever = RAGRetriever(registry[kb_name], db_path=db_path)
    return retriever.retrieve(query, top_k=top_k, mode=mode, **kwargs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="RAG 检索编排器")
    ap.add_argument("--registry", default=str(SCRIPTS_DIR.parent / "registry.yaml"))
    ap.add_argument("--kb", required=True)
    ap.add_argument("query", help="查询文本")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--mode", choices=["hybrid", "semantic", "keyword"], default="hybrid")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--path-filter", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    registry = load_registry(args.registry)
    if args.kb not in registry:
        print(f"ERROR: 知识库 {args.kb!r} 不存在。可用: {', '.join(registry)}", file=sys.stderr)
        sys.exit(1)

    retriever = RAGRetriever(registry[args.kb])
    results = retriever.retrieve(
        args.query,
        top_k=args.top_k,
        mode=args.mode,
        use_rerank=not args.no_rerank,
        path_filter=args.path_filter,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        print(retriever.format_results(results))
