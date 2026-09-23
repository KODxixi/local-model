# 技术决策记录

> 关键技术选型与理由

## 存储与索引

| 决策 | 理由 |
|------|------|
| LanceDB 替代 SQLite | ANN 检索 vs 全表扫描，列式存储压缩率高，无锁并发 |
| 通用文本表与 领域 图文表独立管理 | 向量空间不能混用；领域 的当前状态、后续升级目标与门禁见 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) |
| Table.optimize() 替代 compact_files() | 消除 deprecated warning，使用 LanceDB 推荐的新 API |
| FTS 索引懒加载 | 首次 keyword_search 时自动创建，不需要手动建索引 |

## 检索策略

| 决策 | 理由 |
|------|------|
| **RRF 混合召回** | 语义/关键词分别排名后用 Reciprocal Rank Fusion 融合，避免不同含义的原始分数直接混合 |
| **BM25 中文 ngram** | LanceDB FTS base_tokenizer=ngram(2-4)，中文短语友好，不需要 jieba；失败降级 LIKE |
| **path_filter 服务端预过滤** | `where(contains(path,'...'), prefilter=True)` 在 BM25 排名前过滤，不依赖取数上限 |
| **Qwen 查询指令** | 查询侧加前缀 `Instruct: ...\nQuery: `，文档侧不变不重建向量；可回退 |
| 权重只影响排序不改变 score | 避免权重压垮最终相似度分数，输出保留原始相关性 |
| ~~查询扩展可选（--expand）~~ | **已砍**：该 flag 在 CLI 里已不存在（2026-09-21 核实）。现存的诊断 flag 是 `--explain` / `--trace` |
| 上下文消歧零延迟 | 规则引擎（代词→上下文实体），不调用 LLM，无额外延迟 |
| rerank recall_size=24 | rerank 0.36s/篇是最大瓶颈，截断到24篇使检索延迟 ~9s |
| 稳定去重（chunk_id+内容哈希） | chunk_id 缺失时用 path+完整内容 sha256 前16位，避免同路径不同后文被误合并 |

## 解析与入库

| 决策 | 理由 |
|------|------|
| PDF 入库用 PyMuPDF+WebP+HTML重组 | 文本层优先（快），WebP缓存（增量），HTML重组（保留布局） |
| 不用 PaddleOCR | 对话/视觉主模型一次完成 OCR+描述+分类，不需要单独的 OCR 引擎 |

## 模型与架构

| 决策 | 理由 |
|------|------|
| **30B 从 8080 独立进程迁入 llama-swap**（2026-09-21 甲-1） | 要「自主调度 + 自主卸载 + 调用前判断」就必须单一调度器；llama-swap 原生提供 `ttl` / `groups` / `/running`。**`proxy:` 键不存在** —— llama-swap 只能 spawn、不能代理外部进程，所以「把 8080 挂进去」这条路从根上不成立。代价：`-c 32768 → 16384`（与检索栈共存），打标的 24576 被静默钳到 ~13.5k（实测不影响 —— 产出仅 ~750 token）。8080 实例退役、计划任务 `MuseGlimmer` 置 Disabled（防双开 OOM）。图文、会话、决策与 Skill 消费方的默认端点全部改指 9123 |
| **reranker 用 2B 而非 8B**（2026-09-21） | 8B @`-c4096` 占 6.45 GB，与常驻 30B 相加超订；2B 占 1.36 GB。A/B 实测排序质量无差异（Top-1 6/8 打平，MRR 0.8375 vs 0.8000），代价只是分数区分度变窄（+0.11 vs +0.61），而**没有消费方拿 rerank 分数当阈值**（全部只用名次，RRF 亦按名次加权）。见 `C:\AI\tools\llama-swap\rerank-ab-20260921.md` |
| **两类向量互斥**（2026-09-21） | 单个 embed + rerank 可共存，但两个 embed 相加超检索侧上限（≈8.6 GB，CUDA 后端）。文本检索与图文检索天然不同时发生，故 `exclusive: true` 零代价。数字以 LOCAL-MODELS.md 第四节为准 |
| rag_client 零第三方依赖 | 外部项目可直接 import，不会因 lancedb 缺失而失败 |
| ~~MCP 纯薄入口层~~ | **已终止**（2026-09-21）：MCP 层整体退役，入口收敛到 `cli.py`。原决策的意图（业务逻辑只在 Skill 层）已达成 |
| registry.local.yaml 合并 | 仓库里的注册表保持可公开，本机私有库另存一份不进 git |
