# 技术决策记录

> 关键技术选型与理由

## 存储与索引

| 决策 | 理由 |
|------|------|
| LanceDB 替代 SQLite | ANN 检索 vs 全表扫描，列式存储压缩率高，无锁并发 |
| 文本表4096维 + 图片表2048维分离 | 不同模型维度不同，混在一张表会导致维度冲突 |
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
| 查询扩展可选（--expand） | 提升召回率但增加延迟，默认关闭，需要时显式开启 |
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
| llama-swap Vulkan + 另一路 CUDA | 检索模型用 Vulkan llama.cpp（TTL装卸，4模型共享），视觉/打标用 CUDA + DFlash（性能最优） |
| rag_client 零第三方依赖 | 外部项目可直接 import，不会因 lancedb 缺失而失败 |
| MCP 纯薄入口层 | 消除双轨制，MCP 只做 stdio 协议适配，全部业务逻辑在 Skill 层 |
| registry.local.yaml 合并 | 仓库里的注册表保持可公开，本机私有库另存一份不进 git |
