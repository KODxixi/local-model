"""查询改写与文档增强：用本地 LLM 提升检索质量。

两个核心能力：
1. query_rewrite() — 用本地 LLM 把用户的自然语言查询改写成更适合检索的形式
   （扩展同义词、拆解复合查询、生成多个子查询）
2. summarize_document() — 为文档生成摘要和结构化元数据（标题、标签、关键实体）

这两个能力是"把数据解构成便于 agent 调取的形式"的关键——
不只是存向量，还要存语义摘要和标签，让 agent 更容易命中。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# 对话模型（用于生成式任务；红线：只跑对话/识图，不跑 embed/rerank）
# 可用环境变量 LOCAL_RAG_LLM_BASE 覆盖
DEFAULT_LLM_BASE = os.getenv("LOCAL_RAG_LLM_BASE", "http://127.0.0.1:9123")
# 模型名通过环境变量配置，不写死本机路径
DEFAULT_LLM_MODEL = os.getenv("LOCAL_RAG_LLM_MODEL", "muse-glimmer-30b")
DEFAULT_TIMEOUT = 120

# P1-2: 重试配置（与 rag_client 保持一致）
_LLM_RETRY_MAX = 3
_LLM_RETRY_INITIAL_DELAY = 2.0
_LLM_RETRY_BACKOFF = 2.0
_LLM_RETRY_MAX_DELAY = 30.0

# 生成式任务默认参数
REWRITE_MAX_TOKENS = 512          # 查询改写单次输出上限
SUMMARY_MAX_TOKENS = 512          # 文档摘要单次输出上限
SUMMARY_MAX_CONTENT_CHARS = 4000  # 送入摘要 LLM 的最大正文字符数
MAX_SUB_QUERIES = 3               # 多查询检索最多并发子查询数
#: 生成式任务采样温度（抽取/改写类任务要求稳定输出）
REWRITE_TEMPERATURE = 0.2
SUMMARY_TEMPERATURE = 0.2
#: 多查询检索最终返回条数
MULTI_QUERY_TOP_K = 8


def _llm_chat(
    messages: list[dict[str, str]],
    *,
    base_url: str = DEFAULT_LLM_BASE,
    model: str = DEFAULT_LLM_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """调用本地 LLM（OpenAI-compatible API）。

    P1-2: 加指数退避重试（3 次，initial_delay=2s, backoff=2.0, + jitter），
    应对端点冷加载 / 偶发连接重置。仅对连接错误/超时/5xx/429 重试，
    其他 4xx 立即抛出。推理模型只取 content，忽略 reasoning_content。
    """
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    last_exc: Exception | None = None
    delay = _LLM_RETRY_INITIAL_DELAY
    for attempt in range(1, _LLM_RETRY_MAX + 1):
        try:
            req = urllib.request.Request(
                f"{base_url}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
            return str(result["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_exc = e
            else:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
        except Exception:
            raise

        if attempt < _LLM_RETRY_MAX:
            sleep_for = min(delay + random.uniform(0, delay * 0.3), _LLM_RETRY_MAX_DELAY)
            print(
                f"[rag_enhance] _llm_chat attempt {attempt}/{_LLM_RETRY_MAX} failed: "
                f"{type(last_exc).__name__}, retry in {sleep_for:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
            delay = min(delay * _LLM_RETRY_BACKOFF, _LLM_RETRY_MAX_DELAY)

    raise RuntimeError(
        f"LLM 调用失败（已重试 {_LLM_RETRY_MAX} 次）: {base_url} — "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# 查询改写
# ---------------------------------------------------------------------------

@dataclass
class RewrittenQuery:
    """改写后的查询。"""
    original: str
    rewritten: str  # 优化后的主查询
    sub_queries: list[str] = field(default_factory=list)  # 拆解的子查询
    keywords: list[str] = field(default_factory=list)  # 提取的关键词
    reasoning: str = ""  # 改写理由（调试用）


def rewrite_query(
    query: str,
    *,
    context: str = "",
    base_url: str = DEFAULT_LLM_BASE,
    model: str = DEFAULT_LLM_MODEL,
) -> RewrittenQuery:
    """用本地 LLM 改写查询，提升检索命中率。

    改写策略：
    1. 扩展同义词和相关术语
    2. 拆解复合查询为多个子查询
    3. 提取核心关键词
    4. 去除口语化表达，转为检索友好的表述

    Args:
        query: 原始查询
        context: 可选的上下文（如对话历史、知识库描述）
        base_url: LLM 端点
        model: LLM 模型

    Returns:
        RewrittenQuery
    """
    system_prompt = """你是一个检索查询优化专家。你的任务是把用户的自然语言查询改写成更适合语义检索的形式。

规则：
1. 扩展同义词和专业术语（用户说"房子"，可能也指"住宅"、"户型"、"居住空间"）
2. 拆解复合查询（用户问"A和B的区别"，拆成"A"、"B"两个子查询）
3. 提取3-5个核心关键词
4. 去除口语化、疑问句式，转为陈述式检索词
5. 保持中文，不要翻译成英文

输出格式（严格JSON，不要其他文字）：
{
  "rewritten": "优化后的主查询",
  "sub_queries": ["子查询1", "子查询2"],
  "keywords": ["关键词1", "关键词2", "关键词3"],
  "reasoning": "简短的改写理由"
}"""

    user_prompt = f"原始查询：{query}"
    if context:
        user_prompt += f"\n\n上下文：{context}"

    try:
        response = _llm_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            base_url=base_url,
            model=model,
            temperature=REWRITE_TEMPERATURE,
            max_tokens=REWRITE_MAX_TOKENS,
        )
        # 解析 JSON（LLM 可能输出 markdown 代码块）
        response = re.sub(r"^```json\s*", "", response)
        response = re.sub(r"\s*```$", "", response)
        data = json.loads(response)
        return RewrittenQuery(
            original=query,
            rewritten=data.get("rewritten", query),
            sub_queries=data.get("sub_queries", []),
            keywords=data.get("keywords", []),
            reasoning=data.get("reasoning", ""),
        )
    except Exception as exc:
        # LLM 失败，返回原始查询
        return RewrittenQuery(
            original=query,
            rewritten=query,
            sub_queries=[],
            keywords=[t for t in re.split(r"\s+", query) if len(t) >= 2],
            reasoning=f"LLM rewrite failed: {exc}",
        )


def multi_query_retrieve(
    query: str,
    retrieve_fn: Callable[..., list[Any]],
    *,
    use_rewrite: bool = True,
    top_k: int = MULTI_QUERY_TOP_K,
    **kwargs,
) -> list[Any]:
    """多查询检索：用改写后的多个查询分别检索，合并去重。

    Args:
        query: 原始查询
        retrieve_fn: 检索函数 (query, top_k, **kwargs) -> results
        use_rewrite: 是否使用查询改写
        top_k: 最终返回数量
        **kwargs: 传给 retrieve_fn 的其他参数

    Returns:
        合并去重后的结果列表
    """
    if not use_rewrite:
        return list(retrieve_fn(query, top_k=top_k, **kwargs))

    rewritten = rewrite_query(query)
    all_queries = [rewritten.rewritten] + rewritten.sub_queries
    # 去重查询
    seen = set()
    unique_queries = []
    for q in all_queries:
        if q and q not in seen:
            seen.add(q)
            unique_queries.append(q)

    all_results = []
    for q in unique_queries[:MAX_SUB_QUERIES]:
        results = retrieve_fn(q, top_k=top_k, **kwargs)
        all_results.extend(results)

    # 去重（按 path + text 前100字）
    seen_keys = set()
    unique_results = []
    for r in all_results:
        key = getattr(r, "path", "") + getattr(r, "text", "")[:100]
        if key not in seen_keys:
            seen_keys.add(key)
            unique_results.append(r)

    return unique_results[:top_k]


# ---------------------------------------------------------------------------
# 文档摘要与元数据增强
# ---------------------------------------------------------------------------

@dataclass
class DocumentSummary:
    """文档摘要。"""
    title: str
    summary: str  # 2-3句话摘要
    tags: list[str] = field(default_factory=list)  # 标签
    key_entities: list[str] = field(default_factory=list)  # 关键实体
    content_type: str = ""  # 文档类型（报告/笔记/代码/设计文档等）
    language: str = "zh"


def summarize_document(
    content: str,
    *,
    title: str = "",
    max_content_chars: int = SUMMARY_MAX_CONTENT_CHARS,
    base_url: str = DEFAULT_LLM_BASE,
    model: str = DEFAULT_LLM_MODEL,
) -> DocumentSummary:
    """为文档生成摘要和结构化元数据。

    Args:
        content: 文档内容
        title: 文档标题（可选）
        max_content_chars: 送入 LLM 的最大字符数
        base_url: LLM 端点
        model: LLM 模型

    Returns:
        DocumentSummary
    """
    # 截断过长内容
    truncated = content[:max_content_chars]
    if len(content) > max_content_chars:
        truncated += "\n...(内容已截断)"

    system_prompt = """你是一个文档分析专家。为给定的文档生成结构化元数据。

输出格式（严格JSON，不要其他文字）：
{
  "title": "文档标题（如果原文没有明确标题，根据内容生成一个简洁标题）",
  "summary": "2-3句话的内容摘要，涵盖核心观点",
  "tags": ["标签1", "标签2", "标签3", "标签4", "标签5"],
  "key_entities": ["关键实体1", "关键实体2", "关键实体3"],
  "content_type": "文档类型（如：技术笔记/设计文档/会议纪要/代码/报告/新闻/教程等）",
  "language": "zh或en"
}

规则：
- tags 用名词短语，不用句子
- key_entities 提取人名、地名、项目名、产品名、技术名等
- summary 要具体，不要空泛"""

    user_prompt = f"文档标题：{title or '(无)'}\n\n文档内容：\n{truncated}"

    try:
        response = _llm_chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            base_url=base_url,
            model=model,
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        response = re.sub(r"^```json\s*", "", response)
        response = re.sub(r"\s*```$", "", response)
        data = json.loads(response)
        return DocumentSummary(
            title=data.get("title", title),
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            key_entities=data.get("key_entities", []),
            content_type=data.get("content_type", ""),
            language=data.get("language", "zh"),
        )
    except Exception as exc:
        return DocumentSummary(
            title=title or "(untitled)",
            summary=f"[摘要生成失败: {exc}]",
            tags=[],
            key_entities=[],
            content_type="unknown",
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="查询改写与文档增强")
    sub = ap.add_subparsers(dest="command")

    p_rewrite = sub.add_parser("rewrite", help="改写查询")
    p_rewrite.add_argument("query", help="原始查询")
    p_rewrite.add_argument("--context", default="")

    p_summary = sub.add_parser("summary", help="文档摘要")
    p_summary.add_argument("file", help="文档路径")
    p_summary.add_argument("--title", default="")

    args = ap.parse_args()

    if args.command == "rewrite":
        rw = rewrite_query(args.query, context=args.context)
        print(json.dumps({
            "original": rw.original,
            "rewritten": rw.rewritten,
            "sub_queries": rw.sub_queries,
            "keywords": rw.keywords,
            "reasoning": rw.reasoning,
        }, ensure_ascii=False, indent=2))
    elif args.command == "summary":
        content = Path(args.file).read_text(encoding="utf-8", errors="replace")
        ds = summarize_document(content, title=args.title)
        print(json.dumps({
            "title": ds.title,
            "summary": ds.summary,
            "tags": ds.tags,
            "key_entities": ds.key_entities,
            "content_type": ds.content_type,
            "language": ds.language,
        }, ensure_ascii=False, indent=2))
    else:
        ap.print_help()
