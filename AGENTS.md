# AGENTS.md — local-models 项目操作指南

> 本文件是 agent 操作本项目的权威参考。所有 agent 必须遵守。
> **路径约定**：`<SKILL_ROOT>` = skill 层所在目录（公开仓库里就是 `<仓库根>/skill`）。

## 🚀 给 Agent 的快速调用指南（30 秒上手）

**一句话**：把任意杂乱文件变成 agent 可命中的语义知识库。

```bash
# 1. 诊断系统状态（先跑这个）
.venv\Scripts\python.exe scripts\cli.py doctor

# 2. 建索引（增量）
.venv\Scripts\python.exe scripts\cli.py --kb my_docs index

# 3. 检索
.venv\Scripts\python.exe scripts\cli.py --kb my_docs retrieve "查询内容"

# 4. 解析文件
.venv\Scripts\python.exe scripts\cli.py ingest <文件路径> --json
```

**红线**：
- embed/rerank → 永远只走 **9123**（llama-swap）
- 对话/视觉 → 走 **8080**（Muse Glimmer 30B）
- 索引是可再生投影，**永不反向写**

---

## 项目本质

本地多模态 RAG 系统。**不是**一个独立 MCP，而是**薄入口 + 实现层**的分层架构：

- `mcp/` = **纯 stdio 适配层（薄入口）**：只做协议解析 + 参数校验 + 调用 Skill 公共 API + JSON 输出。
  embed/rerank/索引/检索全部委托 Skill 层，本层不重复实现。
- `skill/` = **唯一实现层（RAG 编排）**：ingest→chunk→embed→index→retrieve，所有业务逻辑在这里。

`mcp/backends/` 只保留 Skill 层没有的能力（外部图文检索 HTTP 适配、共享重试、模型管理）。
`mcp/indexer.py` 是 DEPRECATED 的旧 SQLite 索引器，`server.py` 不引用。

agent 日常使用走 `skill/scripts/cli.py` 统一入口，不需要直接调用 MCP；MCP 仅供其他 agent 框架跨语言复用。

## 首次使用

```powershell
# 在仓库根目录执行（脚本自己定位所在目录，不依赖 cwd）
powershell -ExecutionPolicy Bypass -File <SKILL_ROOT>\setup.ps1
```

安装完成后用 venv Python 运行所有命令（下面示例都省略这个前缀）：

```powershell
<SKILL_ROOT>\.venv\Scripts\python.exe <SKILL_ROOT>\scripts\cli.py doctor
```

> 跑测试前还要补 `pip install pytest ruff`——`setup.ps1` 只装运行依赖。

## 常用操作

| 操作 | 命令 |
|------|------|
| 诊断系统状态 | `cli.py doctor`（不需要 `--kb`） |
| 建索引（增量） | `cli.py --kb <name> index` |
| 全量重建 | `cli.py --kb <name> index --force` |
| 文本检索（默认 hybrid+智能权重） | `cli.py --kb <name> retrieve "<query>"` |
| 跨库检索 | `cli.py --kb all retrieve "<query>"` |
| 查询扩展（提升召回率） | `cli.py --kb <name> retrieve "<query>" --expand` |
| 多轮上下文消歧 | `cli.py --kb <name> retrieve "<query>" --context "上一轮对话"` |
| MMR 多样性重排 | `cli.py --kb <name> retrieve "<query>" --mmr` |
| 自动路由（选 semantic/keyword/hybrid） | `cli.py --kb <name> retrieve "<query>" --auto-route` |
| Parent-Child 检索 | `cli.py --kb <name> retrieve "<query>" --parent-child` |
| 以图搜图 | `cli.py --kb <name> search-image <图片路径>` |
| 检查新鲜度 | `cli.py --kb <name> freshness` |
| 优化索引 | `cli.py --kb <name> optimize` |

**`--kb` 是必填的**，且必须写在子命令**之前**（`--kb`/`--db`/`--registry`/`--json` 都是全局参数）。

## 红线（必须遵守）

1. **embed/rerank 永远只走检索端点（llama-swap 9123）**；对话/视觉端点只跑对话与识图
2. 禁止把 `text-embedding-*` / `*-reranker-*` load 进对话/视觉端点
3. 检索不通时先 `curl 127.0.0.1:9123/running`，绝不靠改端点到别处应急
4. 索引是**可再生投影**，真相源是原始文件；永不反向写
5. 不读取、输出或改写凭据；不扫描 private 目录
6. **显存优先级：向量/检索模型 > 30B**；所有本地模型按需调用、不常驻；30B 只在打标时按需起、用完停。两个都要在场时**不要自行调度**——立即通知用户，由用户决定启停顺序（2026-09-20）
6. **`registry.yaml` 是唯一真相源**，模型地址/维度/知识库配置只改这里（私有路径写 `registry.local.yaml`），不改代码

## 故障排查

| 现象 | 排查步骤 |
|------|----------|
| `ConnectionReset WinError 10054` | 检索端点模型 TTL 卸载，冷加载 ~12s。rag_client 内置 5 次指数退避重试，通常第 2-3 次成功。持续失败检查 `curl 127.0.0.1:9123/running` |
| `ModuleNotFoundError: lancedb` | 依赖未安装。运行 `setup.ps1`，或用 venv Python |
| `向量维度不匹配` | 降级链切换了模型。检查 `registry.yaml` 的 `embed_model` 和 `dimensions` 是否一致 |
| `E_INVALID_ARGS ... 需要 --kb` | 没写 `--kb`。用 `cli.py doctor` 看已注册的库 |
| doctor 命令报错 | doctor 不依赖 lancedb，应始终可用。如 import 阶段崩溃，检查 `cli.py` 顶部延迟导入是否被破坏 |
| 单张图片嵌入返回零向量 | 图片损坏/格式不支持，已被单图失败隔离。用 `all(v==0 for v in vector)` 检测并过滤 |
| 关键词检索返回空 | 确认 LanceDB 表有 INVERTED FTS 索引。运行 `cli.py --kb <name> optimize` 重建索引 |
| 语义检索结果排序异常 | 确认用的是最新代码（L2距离越低越相似，已修复排序方向） |
| 检索结果高度相似 | 加 `--mmr` 参数启用多样性重排 |
| 短查询召回率低 | 加 `--expand` 参数启用查询扩展（LLM 生成同义改写） |
| 代词查询（"它"/"这个"）结果差 | 加 `--context "上一轮对话"` 启用上下文消歧 |
| `另一个索引进程正在运行 (PID=…)` | 索引有 PID 文件锁；确认无进程后删 `~/.local-rag/index.lock` |

## 开发工作流

1. 改代码（Skill 层改 `skill/`，MCP 层改 `mcp/`）
2. 跑聚焦检查：
   ```powershell
   <SKILL_ROOT>\.venv\Scripts\python.exe -m pytest skill\tests -m "not integration"   # 不需要任何服务在跑
   <SKILL_ROOT>\.venv\Scripts\python.exe -m ruff check skill\scripts skill\tests
   ```
3. `integration` 用例需要 llama-swap / 对话模型在线，离线会自动跳过（不用手动 `-m`）
4. 提交 PR

**改动边界**：`skill/registry.yaml` 只放示例配置；不要把本机真实路径、私有库名、个人目录写进任何被 git 跟踪的文件。

## 文档与发布（维护者）

本仓库是公开镜像，**同步 = 纯拷贝，不要在本仓库手工改副本**（两边内容必须完全一致，否则必然打架）：

| 本仓库路径 | 来源 |
|---|---|
| `README.md` / `AGENTS.md` | skill 层目录根的 `README.md` / `AGENTS.md` |
| `skill/**` | skill 层目录其余全部（含 `.gitignore`、`registry.local.yaml` 不进 git） |
| `mcp/**` | MCP 层目录 |
| `LICENSE`、`.github/workflows/`、根 `.gitignore` | **仅本仓库所有**，母库无对应文件 |

公开化规则：所有个人绝对路径写成 `<SKILL_ROOT>` / `<MODELS_DIR>` / `<LLAMA_CPP_DIR>` 占位符；
真实路径只出现在 `registry.local.yaml`（已 gitignore）；私有库名用 `my_docs` 之类的示例名代替。
发布前自检：在仓库根执行 `git grep -nE "C:\\\\Users|<你的私有目录名>"` 应为 0 命中。

## 关键技术决策

| 决策 | 理由 |
|------|------|
| LanceDB 替代 SQLite | ANN 检索 vs 全表扫描，列式存储压缩率高，无锁并发 |
| 文本表4096维 + 图片表2048维分离 | 不同模型维度不同，混在一张表会导致维度冲突 |
| PDF 入库用 PyMuPDF+WebP+HTML重组 | 文本层优先（快），WebP缓存（增量），HTML重组（保留布局） |
| 不用 PaddleOCR | 对话/视觉主模型一次完成 OCR+描述+分类，不需要单独的 OCR 引擎 |
| llama-swap Vulkan + 另一路 CUDA | 检索模型用 Vulkan llama.cpp（TTL装卸，4模型共享），视觉/打标用 CUDA + DFlash（性能最优） |
| rag_client 零第三方依赖 | 外部项目可直接 import，不会因 lancedb 缺失而失败 |
| MCP 纯薄入口层 | 消除双轨制，MCP 只做 stdio 协议适配，全部业务逻辑在 Skill 层 |
| **RRF 混合召回** | 语义/关键词分别排名后用 Reciprocal Rank Fusion 融合，避免不同含义的原始分数直接混合 |
| **BM25 中文 ngram** | LanceDB FTS base_tokenizer=ngram(2-4)，中文短语友好，不需要 jieba；失败降级 LIKE |
| **path_filter 服务端预过滤** | `where(contains(path,'...'), prefilter=True)` 在 BM25 排名前过滤，不依赖取数上限 |
| **Qwen 查询指令** | 查询侧加前缀 `Instruct: ...\nQuery: `，文档侧不变不重建向量；可回退 |
| 权重只影响排序不改变 score | 避免权重压垮最终相似度分数，输出保留原始相关性 |
| 查询扩展可选（--expand） | 提升召回率但增加延迟，默认关闭，需要时显式开启 |
| 上下文消歧零延迟 | 规则引擎（代词→上下文实体），不调用 LLM，无额外延迟 |
| Table.optimize() 替代 compact_files() | 消除 deprecated warning，使用 LanceDB 推荐的新 API |
| rerank recall_size=24 | rerank 0.36s/篇是最大瓶颈，截断到24篇使检索延迟 ~9s |
| 稳定去重（chunk_id+内容哈希） | chunk_id 缺失时用 path+完整内容 sha256 前16位，避免同路径不同后文被误合并 |
| FTS 索引懒加载 | 首次 keyword_search 时自动创建，不需要手动建索引 |
| registry.local.yaml 合并 | 仓库里的注册表保持可公开，本机私有库另存一份不进 git |

## 模型清单

| 模型 | 用途 | 维度 | 端点 |
|------|------|------|------|
| text-embedding-qwen3-embedding-8b | 文字向量 | 4096 | 检索端点（llama-swap 9123） |
| vl-embedding-2b | 图向量 | 2048 | 检索端点（llama-swap 9123） |
| text-reranker-8b | 文本精排 | - | 检索端点（llama-swap 9123） |
| vl-reranker-2b | 图文精排 | - | 检索端点（llama-swap 9123） |
| **Muse Glimmer 30B + DFlash + Vision** | **视觉/打标主模型（按需起，非常驻）** | - | **8080 (CUDA + DFlash)** |

**Muse Glimmer 30B（视觉/打标主模型）：**
- 上下文：131,072 tokens（128K 原生；批量打标场景用 32768）
- 生成速度：125-220 tok/s（DFlash 投机解码）
- GPU：22-30 GB / 32.6 GB
- **能力：文本对话 + 视觉理解（Vision）+ 工具调用（Tool use）**
- 启动命令：见 `README.md` / `SKILL.md` 的 "Muse Glimmer 30B + DFlash" 部分（必须加 `--mmproj` 启用视觉）
- 推荐参数：max_tokens=16384, temperature=0.5, top_p=0.9, n_max=10

**视觉打标架构（单模型直连）：**
- 视觉主模型直接看图打标（30B 的视觉理解能力更强）
- 单模型完成：看图 → 理解 → 输出结构化标签
- 推理模型只取 `content` 字段，忽略 `reasoning_content`
