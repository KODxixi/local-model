"""可视化模块：检索推理链 + 知识图谱 HTML 生成。

两个独立的 HTML 生成器：
- render_retrieval_trace(): 检索全过程时间线（查询→召回→融合→rerank→结果）
- render_knowledge_graph(): vis.js 交互式实体关系网络

设计原则：
- 单文件 HTML（内联 CSS/JS），双击即可在浏览器打开
- 不依赖外部服务（vis.js 用 CDN，离线时降级为静态表格）
- 数据用 JSON 嵌入，前端渲染
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 共用 HTML 骨架
# ---------------------------------------------------------------------------

_PAGE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Microsoft YaHei", sans-serif;
  background: #0d1117; color: #c9d1d9; line-height: 1.6; padding: 24px;
}
h1 { font-size: 22px; margin-bottom: 8px; color: #58a6ff; }
h2 { font-size: 16px; margin: 24px 0 12px; color: #79c0ff; border-left: 3px solid #58a6ff; padding-left: 10px; }
.meta { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
.card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 16px; margin-bottom: 12px;
}
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; margin-right: 6px;
}
.badge-query { background: #1f6feb33; color: #58a6ff; }
.badge-semantic { background: #23863633; color: #3fb950; }
.badge-keyword { background: #d2992233; color: #d29922; }
.badge-fusion { background: #8957e533; color: #a371f7; }
.badge-rerank { background: #f8514933; color: #f85149; }
.badge-result { background: #2ea04333; color: #3fb950; }
.collapsible { cursor: pointer; user-select: none; }
.collapsible::before { content: "▶ "; font-size: 10px; color: #8b949e; }
.collapsible.open::before { content: "▼ "; }
.collapse-content { display: none; margin-top: 10px; }
.collapse-content.show { display: block; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #21262d; }
th { color: #8b949e; font-weight: 600; background: #0d1117; position: sticky; top: 0; }
tr:hover { background: #1c2128; }
.score { font-family: monospace; color: #79c0ff; }
.path { color: #8b949e; font-size: 12px; word-break: break-all; }
.text-preview { color: #c9d1d9; font-size: 13px; max-height: 60px; overflow: hidden; }
.bar-container { background: #21262d; border-radius: 4px; height: 8px; margin-top: 4px; }
.bar { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #58a6ff, #a371f7); }
.timeline { position: relative; padding-left: 28px; }
.timeline::before {
  content: ""; position: absolute; left: 8px; top: 0; bottom: 0;
  width: 2px; background: #30363d;
}
.timeline-item { position: relative; margin-bottom: 16px; }
.timeline-item::before {
  content: ""; position: absolute; left: -24px; top: 6px;
  width: 12px; height: 12px; border-radius: 50%;
  background: #58a6ff; border: 2px solid #0d1117;
}
.timeline-item.semantic::before { background: #3fb950; }
.timeline-item.keyword::before { background: #d29922; }
.timeline-item.fusion::before { background: #a371f7; }
.timeline-item.rerank::before { background: #f85149; }
.timeline-item.result::before { background: #2ea043; }
#graph-container {
  width: 100%; height: 600px; background: #0d1117;
  border: 1px solid #30363d; border-radius: 8px; margin-top: 12px;
}
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; font-size: 12px; }
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 12px; height: 12px; border-radius: 50%; }
.filter-bar { margin: 12px 0; display: flex; gap: 8px; flex-wrap: wrap; }
.filter-btn {
  padding: 4px 12px; border-radius: 16px; border: 1px solid #30363d;
  background: #161b22; color: #c9d1d9; cursor: pointer; font-size: 12px;
}
.filter-btn.active { background: #1f6feb; border-color: #58a6ff; color: #fff; }
"""

_PAGE_JS = """
function toggleCollapse(el) {
  el.classList.toggle('open');
  const content = el.nextElementSibling;
  if (content) content.classList.toggle('show');
}
"""


def _page(title: str, body: str, extra_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
{body}
<script>{_PAGE_JS}
{extra_js}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 1. 检索推理链可视化
# ---------------------------------------------------------------------------

def render_retrieval_trace(trace: dict[str, Any], output_path: str | Path) -> Path:
    """生成检索推理链 HTML。

    Args:
        trace: retrieve(trace=True) 返回的 trace 数据
        output_path: 输出 HTML 路径

    Returns:
        输出文件路径
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    query = trace.get("query", "")
    kb = trace.get("kb", "")
    mode = trace.get("mode", "hybrid")
    total_time = trace.get("total_time_ms", 0)

    steps_html = _render_trace_steps(trace)

    body = f"""
<h1>检索推理链</h1>
<div class="meta">
  知识库: <b>{html.escape(kb)}</b> &nbsp;|&nbsp;
  模式: <b>{html.escape(mode)}</b> &nbsp;|&nbsp;
  总耗时: <b>{total_time:.1f} ms</b> &nbsp;|&nbsp;
  查询: <code>{html.escape(query)}</code>
</div>
<div class="timeline">
{steps_html}
</div>
"""
    html_content = _page(f"检索推理链 - {query[:30]}", body)
    output.write_text(html_content, encoding="utf-8")
    return output


def _render_trace_steps(trace: dict[str, Any]) -> str:
    """渲染时间线各步骤。"""
    steps: list[str] = []

    # Step 0: 查询处理
    qp = trace.get("query_processing", {})
    if qp:
        steps.append(_render_query_step(qp))

    # Step 1: 语义召回
    sem = trace.get("semantic_recall", {})
    if sem:
        steps.append(_render_recall_step(sem, "semantic", "语义召回", "badge-semantic"))

    # Step 2: 关键词召回
    kw = trace.get("keyword_recall", {})
    if kw:
        steps.append(_render_recall_step(kw, "keyword", "关键词召回 (BM25)", "badge-keyword"))

    # Step 3: RRF 融合
    fusion = trace.get("rrf_fusion", {})
    if fusion:
        steps.append(_render_fusion_step(fusion))

    # Step 4: 去重
    dedup = trace.get("deduplication", {})
    if dedup:
        steps.append(_render_dedup_step(dedup))

    # Step 5: rerank
    rerank = trace.get("rerank", {})
    if rerank:
        steps.append(_render_rerank_step(rerank))

    # Step 6: 最终结果
    final = trace.get("final_results", [])
    if final:
        steps.append(_render_final_step(final))

    return "\n".join(steps)


def _render_query_step(qp: dict[str, Any]) -> str:
    lines = ['<div class="timeline-item">']
    lines.append('  <span class="badge badge-query">查询处理</span>')
    lines.append(f'  <b>原始查询:</b> <code>{html.escape(qp.get("original", ""))}</code>')
    if qp.get("disambiguated"):
        lines.append(f'  <br><b>消歧后:</b> <code>{html.escape(qp.get("active_query", ""))}</code>')
    if qp.get("rewritten"):
        lines.append(f'  <br><b>改写后:</b> <code>{html.escape(qp.get("rewritten_query", ""))}</code>')
    if qp.get("expanded_queries"):
        eqs = qp["expanded_queries"]
        lines.append(f'  <br><b>查询扩展 ({len(eqs)} 个):</b>')
        for q in eqs:
            lines.append(f'    <br>&nbsp;&nbsp;• <code>{html.escape(q)}</code>')
    if qp.get("query_instruction"):
        lines.append('  <br><b>Qwen 查询指令:</b> 已启用')
    lines.append('</div>')
    return "\n".join(lines)


def _render_recall_step(
    recall: dict[str, Any], item_class: str, title: str, badge: str
) -> str:
    candidates = recall.get("candidates", [])
    time_ms = recall.get("time_ms", 0)
    lines = [f'<div class="timeline-item {item_class}">']
    lines.append(f'  <span class="badge {badge}">{html.escape(title)}</span>')
    lines.append(f'  <b>{len(candidates)}</b> 条候选 &nbsp;|&nbsp; {time_ms:.1f} ms')
    if recall.get("weights"):
        w = recall["weights"]
        lines.append(f'  &nbsp;|&nbsp; 权重: semantic={w.get("semantic", 0):.2f}, keyword={w.get("keyword", 0):.2f}')
    if candidates:
        lines.append('  <div class="collapsible" onclick="toggleCollapse(this)">查看候选详情</div>')
        lines.append('  <div class="collapse-content">')
        lines.append(_render_candidate_table(candidates, show_rank=True))
        lines.append('  </div>')
    lines.append('</div>')
    return "\n".join(lines)


def _render_fusion_step(fusion: dict[str, Any]) -> str:
    candidates = fusion.get("candidates", [])
    lines = ['<div class="timeline-item fusion">']
    lines.append('  <span class="badge badge-fusion">RRF 融合</span>')
    lines.append(f'  融合后 <b>{len(candidates)}</b> 条 &nbsp;|&nbsp; RRF_K={fusion.get("rrf_k", 60)}')
    w = fusion.get("weights", {})
    if w:
        lines.append(f'  &nbsp;|&nbsp; semantic={w.get("semantic", 0):.2f}, keyword={w.get("keyword", 0):.2f}')
    if candidates:
        lines.append('  <div class="collapsible" onclick="toggleCollapse(this)">查看融合排序</div>')
        lines.append('  <div class="collapse-content">')
        lines.append(_render_candidate_table(candidates, show_rrf=True))
        lines.append('  </div>')
    lines.append('</div>')
    return "\n".join(lines)


def _render_dedup_step(dedup: dict[str, Any]) -> str:
    return f"""<div class="timeline-item">
  <span class="badge badge-fusion">去重</span>
  合并前 <b>{dedup.get("before", 0)}</b> 条 → 合并后 <b>{dedup.get("after", 0)}</b> 条
  &nbsp;|&nbsp; 去重键: chunk_id 优先，缺失用 path+内容哈希
</div>"""


def _render_rerank_step(rerank: dict[str, Any]) -> str:
    results = rerank.get("results", [])
    time_ms = rerank.get("time_ms", 0)
    lines = ['<div class="timeline-item rerank">']
    lines.append('  <span class="badge badge-rerank">Rerank 精排</span>')
    lines.append(f'  输入 <b>{rerank.get("input_count", 0)}</b> 条 → 输出 <b>{len(results)}</b> 条')
    lines.append(f'  &nbsp;|&nbsp; {time_ms:.1f} ms &nbsp;|&nbsp; 模型: {html.escape(rerank.get("model", ""))}')
    if results:
        lines.append('  <div class="collapsible" onclick="toggleCollapse(this)">查看精排结果</div>')
        lines.append('  <div class="collapse-content">')
        lines.append(_render_candidate_table(results, show_rerank=True))
        lines.append('  </div>')
    lines.append('</div>')
    return "\n".join(lines)


def _render_final_step(results: list[dict[str, Any]]) -> str:
    lines = ['<div class="timeline-item result">']
    lines.append('  <span class="badge badge-result">最终结果</span>')
    lines.append(f'  <b>{len(results)}</b> 条')
    lines.append('  <div class="collapsible open" onclick="toggleCollapse(this)">查看最终结果</div>')
    lines.append('  <div class="collapse-content show">')
    lines.append(_render_candidate_table(results, show_final=True))
    lines.append('  </div>')
    lines.append('</div>')
    return "\n".join(lines)


def _render_candidate_table(
    candidates: list[dict[str, Any]],
    *,
    show_rank: bool = False,
    show_rrf: bool = False,
    show_rerank: bool = False,
    show_final: bool = False,
) -> str:
    """渲染候选表格。"""
    headers = ["#", "片段预览", "来源", "分数"]
    if show_rank:
        headers.insert(1, "排名")
    if show_rrf:
        headers.insert(-1, "RRF分")
    if show_rerank:
        headers.insert(-1, "rerank分")
    if show_final:
        headers.insert(-1, "排序方式")

    rows = []
    for i, c in enumerate(candidates, 1):
        text = html.escape(c.get("text", "")[:120])
        path = html.escape(c.get("path", ""))
        score = c.get("score", 0)
        rank = c.get("rank", i)
        rrf = c.get("rrf_score", c.get("_sort_score", 0))
        rerank_score = c.get("rerank_score", score)
        ranked_by = c.get("ranked_by", "")

        cells = [str(i), f'<div class="text-preview">{text}</div>', f'<div class="path">{path}</div>']
        if show_rank:
            cells.insert(1, str(rank))
        if show_rrf:
            cells.insert(-1, f'<span class="score">{rrf:.4f}</span>')
        if show_rerank:
            cells.insert(-1, f'<span class="score">{rerank_score:.4f}</span>')
        if show_final:
            cells.insert(-1, html.escape(ranked_by))
        cells.append(f'<span class="score">{score:.4f}</span>')

        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")

    return f"<table><tr>{''.join(f'<th>{h}</th>' for h in headers)}</tr>{''.join(rows)}</table>"


# ---------------------------------------------------------------------------
# 2. 知识图谱可视化
# ---------------------------------------------------------------------------

# 实体类型 → 颜色
_ENTITY_COLORS: dict[str, str] = {
    "person": "#f85149",
    "place": "#3fb950",
    "project": "#58a6ff",
    "tech": "#d29922",
    "concept": "#a371f7",
    "org": "#f778ba",
    "other": "#8b949e",
}


def render_knowledge_graph(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    output_path: str | Path,
    *,
    kb_name: str = "",
) -> Path:
    """生成知识图谱 vis.js 交互式 HTML。

    Args:
        entities: 实体列表 [{entity_name, entity_type, total_occurrences, description, docs}]
        relations: 关系列表 [{source, target, cooccurrence_count, docs}]
        output_path: 输出 HTML 路径
        kb_name: 知识库名称

    Returns:
        输出文件路径
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 构建 vis.js 节点和边
    nodes = []
    for e in entities:
        etype = e.get("entity_type", "other")
        color = _ENTITY_COLORS.get(etype, _ENTITY_COLORS["other"])
        size = max(10, min(40, 10 + e.get("total_occurrences", 1) * 2))
        nodes.append({
            "id": e["entity_name"],
            "label": e["entity_name"],
            "title": (
                f"<b>{html.escape(e['entity_name'])}</b><br>"
                f"类型: {etype}<br>"
                f"出现次数: {e.get('total_occurrences', 0)}<br>"
                f"文档数: {e.get('doc_count', 0)}<br>"
                f"描述: {html.escape(e.get('description', '')[:100])}"
            ),
            "color": {"background": color, "border": color},
            "size": size,
            "font": {"color": "#c9d1d9", "size": 12},
        })

    edges = []
    for r in relations:
        width = max(1, min(8, r.get("cooccurrence_count", 1)))
        edges.append({
            "from": r["source"],
            "to": r["target"],
            "title": f"共现 {r.get('cooccurrence_count', 0)} 次<br>文档: {len(r.get('docs', []))}",
            "width": width,
            "color": {"color": "#30363d", "highlight": "#58a6ff"},
        })

    # 类型统计
    type_counts: dict[str, int] = {}
    for e in entities:
        t = e.get("entity_type", "other")
        type_counts[t] = type_counts.get(t, 0) + 1

    legend_html = "".join(
        f'<div class="legend-item"><span class="legend-dot" style="background:{_ENTITY_COLORS.get(t, "#8b949e")}"></span>'
        f'{t} ({c})</div>'
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
    )

    filter_buttons = "".join(
        f'<button class="filter-btn" onclick="filterType(\'{t}\')">{t} ({c})</button>'
        for t, c in sorted(type_counts.items(), key=lambda x: -x[1])
    )

    graph_js = f"""
const nodes = new vis.DataSet({json.dumps(nodes, ensure_ascii=False)});
const edges = new vis.DataSet({json.dumps(edges, ensure_ascii=False)});
const container = document.getElementById('graph-container');
const data = {{ nodes, edges }};
const options = {{
  nodes: {{ shape: 'dot', borderWidth: 2 }},
  edges: {{ smooth: {{ type: 'continuous' }} }},
  physics: {{
    barnesHut: {{ gravitationalConstant: -8000, centralGravity: 0.3, springLength: 150, springConstant: 0.04 }},
    stabilization: {{ iterations: 200 }}
  }},
  interaction: {{ hover: true, tooltipDelay: 100 }}
}};
const network = new vis.Network(container, data, options);

let currentFilter = null;
function filterType(type) {{
  if (currentFilter === type) {{
    nodes.update(nodes.get().map(n => ({{ id: n.id, hidden: false }})));
    currentFilter = null;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  }} else {{
    nodes.update(nodes.get().map(n => ({{ id: n.id, hidden: n.color?.background !== '{_ENTITY_COLORS.get("__placeholder__", "")}' && n.id !== undefined ? true : false }})));
    // 重新按类型过滤
    const colorMap = {json.dumps(_ENTITY_COLORS, ensure_ascii=False)};
    const targetColor = colorMap[type] || colorMap['other'];
    nodes.update(nodes.get().map(n => ({{ id: n.id, hidden: n.color.background !== targetColor }})));
    currentFilter = type;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
  }}
}}
"""

    body = f"""
<h1>知识图谱</h1>
<div class="meta">
  知识库: <b>{html.escape(kb_name)}</b> &nbsp;|&nbsp;
  实体: <b>{len(entities)}</b> &nbsp;|&nbsp;
  关系: <b>{len(relations)}</b>
</div>
<div class="legend">{legend_html}</div>
<div class="filter-bar">
  <button class="filter-btn active" onclick="filterType('all')">全部</button>
  {filter_buttons}
</div>
<div id="graph-container"></div>
<h2>实体列表（按出现频率）</h2>
<div class="card">
{_render_entity_table(entities)}
</div>
"""

    extra_js = graph_js + """
document.querySelector('.filter-btn').addEventListener('click', function() {
  if (currentFilter) {
    nodes.update(nodes.get().map(n => ({ id: n.id, hidden: false })));
    currentFilter = null;
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
  }
});
"""

    # 修正 filterType 函数中的占位符
    extra_js = extra_js.replace("'__placeholder__'", "'all'")

    # vis.js CDN：图谱布局依赖联网加载；离线时给出可见提示而不是白屏
    vis_cdn = """<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<script>
window.addEventListener('load', function () {
  if (window.vis) return;
  document.body.insertAdjacentHTML(
    'afterbegin',
    '<p style="margin:0;padding:12px;background:#fee;color:#900;font-family:system-ui">' +
    'vis-network 未能从 CDN 加载（离线环境）：图谱布局不可用，数据仍在上面的表格与页面内嵌 JSON 中。' +
    '</p>'
  );
});
</script>"""

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知识图谱 - {html.escape(kb_name)}</title>
<style>{_PAGE_CSS}</style>
{vis_cdn}
</head>
<body>
{body}
<script>{_PAGE_JS}
{extra_js}</script>
</body>
</html>"""

    output.write_text(html_content, encoding="utf-8")
    return output


def _render_entity_table(entities: list[dict[str, Any]]) -> str:
    """渲染实体列表表格。"""
    rows = []
    for i, e in enumerate(entities[:50], 1):
        etype = e.get("entity_type", "other")
        color = _ENTITY_COLORS.get(etype, _ENTITY_COLORS["other"])
        name = html.escape(e["entity_name"])
        desc = html.escape(e.get("description", "")[:80])
        occ = e.get("total_occurrences", 0)
        docs = e.get("doc_count", 0)
        rows.append(
            f"<tr><td>{i}</td>"
            f'<td><span class="legend-dot" style="background:{color};display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;"></span>'
            f"<b>{name}</b></td>"
            f"<td>{etype}</td><td>{occ}</td><td>{docs}</td>"
            f'<td style="color:#8b949e;font-size:12px;">{desc}</td></tr>'
        )
    return (
        "<table><tr><th>#</th><th>实体</th><th>类型</th><th>出现次数</th><th>文档数</th><th>描述</th></tr>"
        + "".join(rows)
        + "</table>"
    )
