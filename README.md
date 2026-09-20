# local-models-mcp

本地多模态 RAG 系统。MCP 做原子工具层，Skill 做 RAG 编排层。全链路本地运行，所有模型跑在 GPU 上。

## 架构图

> **路径约定**：下文命令中的 `<SKILL_ROOT>` 指本 skill 目录（公开仓库里就是 `<仓库根>/skill`），
> `<MODELS_DIR>` 指你存放 GGUF 的目录，`<LLAMA_CPP_DIR>` 指 llama.cpp 二进制目录。
> 私有知识库路径建议写进 `registry.local.yaml`（不进 git），不要改本仓库跟踪的文件。

```mermaid
flowchart TB
    IN["用户文件<br/>PDF / Word / Excel / PPT / 图片 / 文本"]

    subgraph SKILL["skill/ · RAG 编排层"]
        direction TB
        ING["ingest.py<br/>文件解析"]
        CHK["chunker.py<br/>智能分块"]
        IDX["rag_indexer.py<br/>索引编排（增量 + 新鲜度 + 知识图谱）"]
        RET["rag_retriever.py<br/>混合召回 → RRF → rerank"]
        KG["knowledge_graph.py<br/>实体 + 关系"]
        ENH["rag_enhance.py<br/>查询改写 / 摘要"]
        ING --> CHK --> IDX
    end

    subgraph MCP["mcp/ · 纯 stdio 适配层（薄入口，不实现业务）"]
        SRV["server.py（FastMCP）<br/>search / index / list_kbs → 委托 skill<br/>embed / rerank / status → rag_client<br/>ocr、list/load/unload_model 独立保留"]
    end

    subgraph STORE["向量存储 · LanceDB"]
        T1["kb_{name}<br/>文本 4096 维"]
        T2["kb_{name}_images<br/>页面图 2048 维"]
        T3["kg_{name}<br/>知识图谱"]
    end

    subgraph MODELS["本地模型服务（GPU）"]
        LS["llama-swap 127.0.0.1:9123<br/>Vulkan llama.cpp · TTL 自动装卸<br/>text-embedding-qwen3-embedding-8b 4096<br/>text-reranker-8b<br/>vl-embedding-2b 2048<br/>vl-reranker-2b"]
        MG["Muse Glimmer 127.0.0.1:8080<br/>CUDA + DFlash + Vision<br/>对话 / OCR / 视觉理解"]
    end

    IN --> SKILL
    IDX -->|embed_images| STORE
    IDX -->|embed_texts| STORE
    RET --> STORE
    KG --> T3
    RET -->|embed + rerank| LS
    IDX -->|embed| LS
    ING -->|扫描页 OCR / 图片理解| MG
    ENH -->|查询改写 / 摘要| MG
    SKILL --> MCP
    MCP --> MODELS
```

**三层职责：** `skill/` 是唯一实现层（所有业务逻辑）；`mcp/` 只做 stdio 协议适配 +
参数校验 + 调用 Skill 公共 API；`backends/` 只保留 Skill 层没有的能力（外部图文检索、
共享重试、模型管理）。详见 [`AGENTS.md`](AGENTS.md)。


## 模型选型

| 用途 | 模型 | 维度 | 后端 | GPU |
|---|---|---|---|---|
| **文字向量** | text-embedding-qwen3-embedding-8b (Q4_K_M) | 4096 | llama-swap 9123 | Vulkan, -ngl 99 |
| **图向量** | vl-embedding-2b | 2048 | llama-swap 9123 | Vulkan, -ngl 99 |
| **文本精排** | text-reranker-8b | - | llama-swap 9123 | Vulkan, -ngl 99 |
| **图文精排** | vl-reranker-2b | - | llama-swap 9123 | Vulkan, -ngl 99 |
| **视觉理解** | Muse-Glimmer-30B-Q4_K_M + mmproj | - | Muse Glimmer 8080 | CUDA, --gpu-layers 99 |
| **对话/改写** | Muse-Glimmer-30B-Q4_K_M + DFlash | - | Muse Glimmer 8080 | CUDA + DFlash, --gpu-layers 99 |

**选型理由：**
- 检索模型（embedding/rerank）走 llama-swap：Vulkan 版 llama.cpp，TTL 自动装卸，4个模型共享一套基础设施
- 视觉/对话走 Muse Glimmer 8080：CUDA 13.3 + DFlash 投机解码，128K 上下文，125-220 tok/s，同时做 OCR、描述、分类、对话，不需要 PaddleOCR
- 所有模型全层 GPU（`--gpu-layers 99`），不走 CPU

## 数据库 / 向量库

**LanceDB**（列式存储，无锁并发，ANN 索引）

路径：`~/.local-rag/lancedb/`

### 表结构

每个知识库两个表，按维度分离：

| 表名 | 维度 | 用途 | Schema |
|---|---|---|---|
| `kb_{name}` | 4096 | 文本 chunks | vector, chunk_id, text, path, doc_type, heading_path, section_title, start_line, end_line, mtime_ns, model, metadata |
| `kb_{name}_images` | 2048 | PDF页面/图片 | vector, image_id, path, page_num, description, doc_type, mtime_ns, model, metadata |

### 索引类型

| 索引 | 用途 | 创建方式 |
|---|---|---|
| **IVF_HNSW_SQ** (ANN) | 语义向量检索 | `vector_store._ensure_index()`，建表时自动创建 |
| **INVERTED FTS** (BM25) | 关键词全文检索 | `_ensure_fts_index()`，首次 keyword_search 时懒加载创建 |

**BM25 FTS 配置：**
- `base_tokenizer="ngram"`，`ngram_min_length=2`，`ngram_max_length=4`
- 中文短语友好（按 2-4 字切分，不需要 jieba）
- 英文缩写（GPU/PDF）也能命中
- 失败自动降级到 Pandas LIKE 包含匹配

### 为什么用 LanceDB 而不是 SQLite

| 维度 | SQLite (旧) | LanceDB (新) |
|---|---|---|
| 检索方式 | 全表扫描 + 余弦距离 | ANN (IVF_PQ) |
| 并发 | 单写锁 | 无锁并发 |
| 存储 | 行式 | 列式（压缩率高） |
| 大规模 | 慢（O(n)） | 快（O(log n)） |

### 数据库管理办法

**数据目录结构：**
```
~/.local-rag/
├── lancedb/                    # LanceDB 数据库（向量+全文索引）
│   ├── kb_{name}.lance/        # 文本向量表（4096维）
│   ├── kb_{name}_images.lance/ # 图片向量表（2048维）
│   └── kg_{name}.lance/        # 知识图谱表（实体+关系）
├── pdf-cache/                  # PDF 渲染缓存（WebP）
│   └── <pdf_sha256>/           # 按 PDF 内容哈希分目录
│       ├── page_001.webp
│       ├── page_002.webp
│       └── meta.json           # 渲染参数（DPI/quality/max_edge）
└── tmp/                        # 临时文件（构建/迁移中间产物）
```

**备份策略：**
- 索引是**可再生投影**，真相源是原始文件；不需要定期备份索引
- 需要备份时：复制整个 `~/.local-rag/lancedb/` 目录（LanceDB 是文件级数据库，复制即备份）
- 知识库配置（registry.yaml）在项目目录中，随 git 版本管理

**清理策略：**
- `cli.py --kb <name> index --prune`：索引时自动删除已不存在文件的 chunks（孤儿清理）
- PDF 缓存按 sha256 分目录，PDF 文件删除后缓存不会自动清理；手动删除 `~/.local-rag/pdf-cache/<sha256>/`
- 临时文件 `~/.local-rag/tmp/` 可随时删除（不影响索引）

**迁移流程：**
- 旧 SQLite → LanceDB：`cli.py --kb <name> migrate`（从旧 SQLite 索引读取 chunks 重新写入 LanceDB）
- 维度变更：需要全量重建 `cli.py --kb <name> index --force`（向量维度不可原地修改）
- 模型变更：embed_model 改变时必须 `--force` 重建，否则向量空间不兼容

**损坏恢复：**
- LanceDB 表损坏：删除对应 `.lance/` 目录后 `--force` 重建
- FTS 索引损坏：`cli.py --kb <name> optimize`（重建索引），或删除表后重建
- 并发写入冲突：索引操作有 PID 文件锁，冲突时等待或杀掉旧进程

**版本管理：**
- LanceDB 版本升级后可能需要 `optimize` 重组数据文件
- registry.yaml 中的 `dimensions` 和 `embed_model` 是索引版本的关键标识，变更即需重建

### RAG 库文件夹结构（知识库组织规范）

**知识库根目录规范：**
```
<kb_root>/
├── 00-入口/                    # 索引/导航/README
├── 01-文档/                    # 正式文档（PDF/DOCX/MD）
├── 02-参考/                    # 参考资料（规范/标准/论文）
├── 03-数据/                    # 数据文件（XLSX/CSV/JSON）
├── 04-演示/                    # 演示文稿（PPTX）
├── 05-图片/                    # 图片素材（PNG/JPG/WebP）
└── 99-归档/                    # 旧版本/废弃文件（仍可检索但标记 archived）
```

**命名规范：**
- 目录：`序号-中文名称`（如 `01-文档`），序号控制排序
- 文件：`<项目编号>_<主题>_<版本>.<ext>`（如 `ARCH-001_立面设计_v2.pdf`）
- 避免空格和特殊字符，用 `-` 或 `_` 分隔
- 版本号用 `v1/v2/final`，不用 `最终版/最终版2/真的最终版`

**多库隔离：**
- 每个知识库独立 LanceDB 表（`kb_{name}`），互不干扰
- `--kb all` 跨库检索时分别查询再合并排序
- 知识库配置在 `registry.yaml` 中定义，新增库 = 加一段配置，不改代码

**文件类型覆盖：**
| 类型 | 扩展名 | 解析方式 |
|------|--------|----------|
| 文本 | .md .txt .py .json .yaml | 直接读取 |
| PDF | .pdf | PyMuPDF 文本层 + WebP 渲染 + VLM OCR（扫描件） |
| Word | .docx | python-docx |
| Excel | .xlsx .csv | pandas + openpyxl |
| PPT | .pptx | python-pptx |
| 图片 | .png .jpg .webp | VLM 描述 + 图向量 |

## 召回策略

### 文本检索（默认 hybrid + RRF 融合）

```
查询(query)
    │
    ├──▶ 上下文消歧（可选 --context）: 用上一轮对话消歧代词/省略
    │
    ├──▶ 查询扩展（可选 --expand）: LLM 生成 2-3 个同义改写，多查询召回
    │
    ├──▶ Qwen 查询指令（默认启用）: 查询侧加前缀 "Instruct: ...\nQuery: "
    │     └── 文档侧不变，不重建向量；可回退 retrieve(use_query_instruction=False)
    │
    ├──▶ 语义召回: query(+指令) → embedding(4096维) → LanceDB ANN(IVF_HNSW_SQ) → top N
    │
    ├──▶ 关键词召回: query → LanceDB FTS(BM25, 中文 ngram 2-4) → top N
    │     └── path_filter 用 where(contains(path,'...'), prefilter=True) 服务端预过滤
    │
    ▼
RRF 融合（Reciprocal Rank Fusion）: 语义/关键词分别排名，融合分 = sw*1/(60+rank_sem) + kw*1/(60+rank_kw)
    │
    ▼
稳定去重（chunk_id 优先，缺失用 path+完整内容 sha256 前16位）
    │
    ▼
rerank 精排: text-reranker-8b (cross-encoder) → recall_size=24 篇
    │
    ▼
MMR 多样性重排（可选）: 避免返回结果高度相似
    │
    ▼
agent 友好格式: [序号] 标题路径 (score=0.85, rerank) + source + 完整片段
```

**可选模式：**
- `semantic` — 仅语义检索
- `keyword` — 仅关键词（BM25 FTS，不调用模型，最快）
- `hybrid` — 混合检索 + RRF 融合（默认）

**高级选项：**
- `--expand` — 查询扩展（对话端点生成同义改写，提升召回率，增加延迟）
- `--context "上一轮对话"` — 多轮上下文消歧（零延迟规则引擎，代词→实体）
- `--mode parent-child` — Parent-Child 检索（chunk 检索后返回父文档上下文）
- `--mmr` — MMR 多样性重排
- `--route` — 查询路由（自动选 semantic/keyword/hybrid）
- `path_filter` — 路径包含过滤（服务端预过滤，不依赖取数上限）
- `doc_type_filter` — 文档类型过滤
- `use_query_instruction=False` — 禁用 Qwen 查询指令（回退到原始查询）

### 跨库检索（`--kb all`）

```
查询 → 遍历所有已索引知识库 → 分别检索 → 按 score 合并排序 → top_k
```

无索引或出错的库自动跳过，不影响其他库。

### 以图搜图（`search-image`）

```
查询图片 → embed_images() (vl-embedding-2b, 2048维) → LanceDB ANN → top_k
```

返回：图片路径、页码、webp 缓存路径、页面描述、相似度 score。

### 知识图谱补充

向量检索找"语义相似"，知识图谱找"实体关联"：
- `kg extract <file>` — 从文件提取实体（LLM + 规则双路）
- `kg find "LanceDB"` — 找提到某实体的文档
- `kg related "Python"` — 找共现实体
- `kg list --type tech` — 按类型列出实体

## PDF 入库策略

```
PDF 文件
    │
    ├──▶ PyMuPDF 提取每页文本层
    │
    ├──▶ 每页渲染为 WebP (144 DPI, quality=85, max_edge=2400)
    │     └── 增量缓存: ~/.local-rag/pdf-cache/<pdf_sha256>/page_XXX.webp
    │
    ├──▶ 文本 < 20字符的页面 → VLM (Muse Glimmer 30B Vision) 做 OCR + 描述
    │
    ├──▶ HTML 重组: 每页 = <img webp> + <div class="page-text">文本</div>
    │
    ├──▶ 智能分块（按标题/段落，非固定字符数）
    │
    ├──▶ 文字向量 (4096维) → kb_{name} 表
    │
    └──▶ 页面图向量 (2048维) → kb_{name}_images 表
```

**关键设计：**
- WebP 缓存按 PDF 的 sha256 分目录，文件未变更则跳过渲染
- 文本层优先，只有扫描件/图片页才调 VLM（节省 GPU 资源）
- 每页同时存文字向量和图向量，支持文本检索和以图搜图

## 可靠性设计

### 指数退避重试（适配 llama-swap TTL 冷加载）

llama-swap 模型 TTL 300s 自动卸载后，首次请求会报 `ConnectionReset (WinError 10054)`，模型冷加载需 ~12s。

`rag_client._request_json` 内置重试：
- **5次重试**，指数退避：2s → 4s → 8s → 16s → 30s（总等待约60s）
- 仅重试：连接错误 / 超时 / 5xx / 429 Too Many Requests
- 4xx 立即抛出（不重试无效请求）
- 重试日志输出到 stderr，便于排查

### 降级链维度校验

llama-swap 不可用时可能降级到其他 embedding 后端（如其他后端 768维 / API 1536维），但向量表是按 4096 维创建的。

**防护机制：**
- `embed_texts(include_dimensions=True)` 返回 `(vectors, dimensions)`，调用方可检测维度变化
- `vector_store.upsert_chunks` 写入前校验维度，不匹配则抛明确 `ValueError`：
  ```
  向量维度不匹配: 表 'kb_my_docs' 定义 4096 维, 实际输入 768 维。
  可能是降级链切换了模型（如 llama-swap 4096维 → 其他后端（如 768维））。
  请检查 registry.yaml 的 embed_model/dimensions 配置...
  ```
- 防止静默写入错误维度数据导致搜索结果异常

### 单图失败隔离

`embed_images()` 预处理阶段逐张隔离失败：
- 单张图片损坏/格式不支持 → 返回零向量（`[0.0]*2048`），不拖垮整个 batch
- 调用方可通过 `all(v == 0 for v in vector)` 检测并过滤失败项
- 批量请求阶段整个 batch 失败时保持零向量，不重试（避免无限循环）

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `ConnectionReset WinError 10054` | llama-swap 模型 TTL 卸载，冷加载约 12–16s。`rag_client` 内置 5 次指数退避重试，通常第 2–3 次成功。持续失败先 `curl 127.0.0.1:9123/running` |
| `ModuleNotFoundError: lancedb` | 依赖没装或用的是系统 Python。跑 `setup.ps1`，或用 `skill\.venv\Scripts\python.exe` |
| `向量维度不匹配: 表定义 4096 维, 实际输入 768 维` | 端点指向了别的 embedding 模型（降级链或改错端口）。核对 `registry.yaml` 的 `embed_model` / `dimensions` 与端点 |
| `unknown kb 'xxx'` / `E_INVALID_ARGS` | 库没注册或 `--kb` 写在了子命令之后。全局参数必须写在子命令**之前** |
| 关键词检索返回空 | 确认 LanceDB 表有 INVERTED FTS 索引，跑 `cli.py --kb <name> optimize` 重建 |
| 语义检索排序异常 | 确认代码是最新的（L2 距离越低越相似，排序方向修过一次） |
| 结果高度相似 | 加 `--mmr` 启用 MMR 多样性重排 |
| 短查询召回低 | 加 `--expand` 启用查询扩展 |
| 代词查询（"它"/"这个"）效果差 | 加 `--context "上一轮对话"` 启用上下文消歧 |
| `另一个索引进程正在运行 (PID=…)` | 索引有 PID 文件锁；确认无进程后删 `~/.local-rag/index.lock` |
| `kg visualize` 生成的页面无图 | 该页面从 CDN 加载 vis-network，离线时会提示而不是白屏；联网后重开即可 |

## 已知限制

- 图片入库慢：每张走一次 VLM（约 10–30s/张），大量图片建索引耗时长
- PDF 扫描页完全依赖 VLM，页数多的 PDF 索引慢
- 单文件默认上限 5MB、单库默认上限 10000 文件（`registry.yaml` 可改）；
  文件数超上限时孤儿清理会自动停用，避免误删未扫描到的文件
- 向量维度不可原地修改：换 `embed_model` 必须 `--force` 全量重建
- 图文检索需要外部图文库提供自己的 LanceDB 索引与 CLI，本仓库不代管

## 安装依赖

首次使用前必须安装依赖（lancedb / pyarrow / PyMuPDF 等）：

```powershell
# 在仓库根目录执行（setup.ps1 自己定位所在目录，不依赖 cwd）
powershell -ExecutionPolicy Bypass -File skill\setup.ps1
```

脚本会创建 `skill\.venv`、装好依赖，并自动跑一次 `doctor` 验证。之后所有命令都用 venv 里的 Python：

```powershell
# 方式1：激活 venv
skill\.venv\Scripts\Activate.ps1
python skill\scripts\cli.py doctor

# 方式2：直接调用 venv Python（无需激活）
skill\.venv\Scripts\python.exe skill\scripts\cli.py doctor
```

**可选：测试与 lint 依赖**（`setup.ps1` 不装，跑测试前必须补上）：

```powershell
skill\.venv\Scripts\python.exe -m pip install pytest ruff
skill\.venv\Scripts\python.exe -m pytest skill\tests -m "not integration"   # 不需要任何服务在跑
```

`cli.py doctor` 会最先检查依赖状态，缺失时明确提示哪些包没装。

## 快速开始

```powershell
# 0. 先注册一个知识库：编辑 skill\registry.yaml，把 my_docs 那段的注释取消、
#    root 改成你自己的目录（推荐写进 registry.local.yaml，见下节）

# 1. 诊断（依赖 / GPU / 模型端点 / 索引覆盖）
skill\.venv\Scripts\python.exe skill\scripts\cli.py doctor

# 2. 建索引（增量，自动跳过未变更文件）
skill\.venv\Scripts\python.exe skill\scripts\cli.py --kb my_docs index

# 3. 文本检索
skill\.venv\Scripts\python.exe skill\scripts\cli.py --kb my_docs retrieve "查询内容"

# 4. 跨库检索（自动搜索所有已索引库）
skill\.venv\Scripts\python.exe skill\scripts\cli.py --kb all retrieve "查询内容"

# 5. 以图搜图（需要 multimodal 类型的库）
skill\.venv\Scripts\python.exe skill\scripts\cli.py --kb my_docs search-image path\to\image.jpg

# 6. 检查索引新鲜度
skill\.venv\Scripts\python.exe skill\scripts\cli.py --kb my_docs freshness
```

> `--kb` 是**必填**的（不再有默认库）：不写会报 `E_INVALID_ARGS` 并提示可用库。

## 统一 CLI 命令清单

> **⚠️ 全局参数位置**：`--kb/--db/--registry` 必须写在**子命令之前**（与 `git`/`kubectl` 一致）。
> ✅ `cli.py --kb my_docs retrieve "query"`　❌ `cli.py retrieve "query" --kb my_docs`（后者报 unrecognized arguments，CLI 会给出提示）。
> 所有子命令均支持 `--json`（默认人类可读文本）。

| 命令 | 用途 |
|---|---|
| `index` | 增量建索引（支持 `--force` 全量重建、`--extract-entities` 知识图谱） |
| `freshness` | 检查索引新鲜度（非零退出码=过期） |
| `stats` | 查看索引统计（chunk数、文件数、文档类型分布） |
| `retrieve` | 混合检索 + rerank（支持 `--kb all` 跨库、`--mode semantic/keyword/hybrid/parent-child`、`--expand` 查询扩展、`--context` 上下文消歧、`--mmr` 多样性、`--route` 自动路由） |
| `search-image` | 以图搜图（vl-embedding-2b 向量检索） |
| `kg extract/find/related/list/stats` | 知识图谱操作 |
| `ingest` | 解析单个文件（调试用，`--json` 输出结构化 Document） |
| `chunk` | 智能分块（调试用） |
| `rewrite` | 查询改写（LLM 扩展同义词+拆解复合查询） |
| `summary` | 文档摘要（标题+摘要+标签+实体） |
| `embed` | 直接调用 embedding（调试用） |
| `rerank` | 直接调用 rerank（调试用） |
| `migrate` | 从旧 SQLite 索引迁移到 LanceDB |
| `optimize` | 优化 LanceDB 表（`Table.optimize()` + ANN 索引重建） |
| `doctor` | 系统诊断（依赖状态+GPU状态+模型可用性+索引覆盖+过期库） |

## 知识库注册表

`skill/registry.yaml` 是唯一真相源。新增库 = 加一段配置：

```yaml
knowledge_bases:
  my_docs:
    type: text                    # text / multimodal
    root: ~/Documents/my-kb
    patterns: ["*.pdf", "*.docx", "*.xlsx", "*.pptx", "*.md"]
    dimensions: 4096
    embed_model: text-embedding-qwen3-embedding-8b
    capability:
      good_for: [文档全文检索]
      example_queries: [某份报告里的数据]
```

### 本机私有库：`registry.local.yaml`（不进 git）

仓库里的 `registry.yaml` 只放示例，你的真实路径放同目录的 `registry.local.yaml`：

```yaml
knowledge_bases:
  my_docs:
    type: text
    root: D:\我的资料\知识库
    patterns: ["*.md", "*.pdf"]
```

加载时它的 `knowledge_bases` 会**合并到 `registry.yaml` 之上**（同名条目以本地文件为准），
所以私有路径永远不进 git，公开仓库也不会因为同步而把你的目录带出去。

### 端点与端口覆盖

优先级：**函数参数 > 环境变量 > registry.yaml > 模块默认值**。

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `LOCAL_RAG_BASE_URL` | embed / rerank 端点（llama-swap） | `http://127.0.0.1:9123` |
| `LOCAL_RAG_VLM_BASE_URL` | 图片 / PDF 扫描页的 VLM 端点 | `http://127.0.0.1:8080` |
| `LOCAL_RAG_LLM_BASE` | 查询改写 / 摘要的 LLM 端点 | `http://127.0.0.1:8080` |

## 外部项目集成

外部项目可直接 import `skill/scripts/` 下的模块，复用 embedding/rerank 客户端和注册表，避免重复实现模型配置、重试逻辑、降级链。

> 注意：`scripts/` 是普通目录不是包名 `local_model_scripts`，**直接按模块名 import**（先把 scripts 目录加入 sys.path），不要写 `from local_model_scripts import ...`（会 ModuleNotFoundError）。

```python
import sys
from pathlib import Path

SKILL_ROOT = Path(r"<SKILL_ROOT>")      # 例：Path(r"C:\tools\local-models\skill")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# rag_client 仅依赖标准库，始终可导入
from rag_client import (
    embed_texts,         # 文本嵌入（带指数退避重试）
    embed_images,        # 图片嵌入（单图隔离+批量请求）
    rerank_texts,        # 文本精排
    health_check,        # 健康检查
)
# rag_indexer 依赖 lancedb/pyarrow（本机已装）
from rag_indexer import load_registry, KBConfig

# 加载注册表（自动合并同目录 registry.local.yaml）
kbs = load_registry(SKILL_ROOT / "registry.yaml")
kb = kbs["my_docs"]
print(f"知识库: {kb.name}, 维度: {kb.dimensions}, 模型: {kb.embed_model}")

# 文本嵌入（返回维度，便于检测降级链变化）
vectors, dims = embed_texts(["hello world"], include_dimensions=True)

# 图片嵌入（支持文件路径 / data URL / raw base64）
img_vectors = embed_images([r"path/to/photo.png"])

# 重排
results = rerank_texts("query", ["doc1", "doc2"], top_k=5)
```

**设计要点：**
- `rag_client` 模块零第三方依赖（只用标准库），外部项目 import 不会因 lancedb 缺失而失败
- `rag_indexer` / `vector_store` 依赖 lancedb，缺失时优雅降级（`load_registry` 仍可用，返回原始 dict）
- 所有 HTTP 请求内置指数退避重试，外部项目无需自己实现

## GPU

所有模型默认跑在 GPU 上（RTX 5090 D 32GB）：

- **llama-swap 9123**：Vulkan llama.cpp，`-ngl 99`（全层），4个检索模型，TTL 300s 自动卸载
- **Muse Glimmer 8080**：CUDA 13.3 + DFlash 投机解码 + Vision，128K 上下文，125-220 tok/s，视觉理解 + 对话生成

### Muse Glimmer 30B + DFlash（对话/Agent 主模型）

**最优配置（已验证，RTX 5090 D 32GB）：**

```powershell
# 先把这两个变量换成你自己的路径
$LlamaDir = "<LLAMA_CPP_DIR>"       # llama.cpp 二进制目录，例：C:\tools\llama.cpp-cuda
$MuseDir  = "<MODELS_DIR>\Muse"     # 模型目录，例：C:\models\Muse

& "$LlamaDir\llama-server.exe" `
    --gpu-layers 99 `
    -m "$MuseDir\Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf" `
    --mmproj "$MuseDir\mmproj-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-type draft-dflash `
    --spec-draft-model "$MuseDir\dflash-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-draft-n-max 10 `
    -fa on -c 131072 --threads 20 `
    --port 8080 --host 127.0.0.1
```

**性能指标（实测）：**
- 上下文：131,072 tokens（128K 原生最大）
- 生成速度：**125-220 tok/s**（DFlash 投机解码）
- GPU 显存：22-30 GB / 32.6 GB
- **能力：文本对话 + 视觉理解（Vision）+ 工具调用（Tool use）**
- 对比：普通 CUDA 73.7 tok/s，Vulkan 30-36 tok/s

**CUDA 环境要求：**
- CUDA 13.3 运行时（DLL 在 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\x64\`）
- DLL 必须复制到 llama.cpp 目录（否则找不到 GPU）：
  - `cudart64_13.dll`
  - `cublas64_13.dll`
  - `cublasLt64_13.dll`
  - `nvblas64_13.dll`

**API 调用：**
- OpenAI 兼容：`http://127.0.0.1:8080/v1/chat/completions`
- model 参数：主模型完整路径

### Agent 调用推荐配置（杂活 + 代码 + 长上下文）

**场景**：作为 Agent 后端，处理各种杂活、代码任务、长文档对话

| 参数 | 值 | 理由 |
|------|-----|------|
| 上下文 | 131,072 (128K) | 代码仓库大，必须拉满 |
| max_tokens | 16,384 (16K) | 平衡：比 32K 快，比 4K 够长 |
| temperature | 0.5 | 平衡：代码要准确，杂活要灵活 |
| top_p | 0.9 | 配合低 temperature 更稳定 |
| DFlash n_max | 10 | 平衡速度和准确性（原 15 偏高） |

**示例请求：**
```json
{
    "model": "<MODELS_DIR>\\Muse\\Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf",
    "messages": [{"role": "user", "content": "重构这段代码..."}],
    "max_tokens": 16384,
    "temperature": 0.5,
    "top_p": 0.9
}
```

**性能指标（实测）：**
- 生成速度：**125-220 tok/s**（DFlash 投机解码）
- 128K 上下文：模型可读取整个代码仓库
- 推理模型：先生成 reasoning_content，再生成 content

用 `cli.py doctor` 一键确认 GPU 型号、驱动、利用率、显存占用和当前加载模型。

## 红线

1. embed/rerank **永远只走 llama-swap 9123**；Muse Glimmer 8080 **只跑对话/视觉理解**
2. 禁止把 `text-embedding-*` / `*-reranker-*` load 进 Muse Glimmer 8080
3. 检索不通时先 `curl 127.0.0.1:9123/running`，绝不靠改端点到 Muse Glimmer 应急
4. 索引是**可再生投影**，真相源是原始文件；永不反向写
5. 不读取、输出或改写凭据；不扫描 private 目录

## 目录结构

<仓库根>/
├── README.md                    # 本文件
├── AGENTS.md                    # agent 操作指南
├── LICENSE                      # MIT
├── .github/workflows/test.yml   # CI：pytest -m "not integration"
├── skill/                       # RAG 编排层
│   ├── SKILL.md                 # agent 指南（skill 运行时读本文件）
│   ├── setup.ps1                # 一键安装（venv+依赖+验证）
│   ├── registry.yaml            # 知识库注册表（唯一真相源，只放示例）
│   ├── registry.local.yaml      # 本机私有库（不进 git，默认不存在）
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── kb-manifest-schema.md    # 垂类库 manifest 规范
│   ├── references/
│   │   └── full-workflow.md     # 底层实现参考（配置陷阱/冷加载/Vulkan vs CUDA）
│   ├── scripts/
│   │   ├── __init__.py          # 公共 API（外部项目可直接 import）
│   │   ├── cli.py               # 统一入口（15 个顶层子命令，kg 含 5 叶子，共 19 叶子命令）
│   │   ├── ingest.py            # 文件解析（PDF WebP+HTML重组/VLM/全格式）
│   │   ├── chunker.py           # 智能分块（按标题/段落）
│   │   ├── rag_indexer.py       # 索引编排（增量+新鲜度+KG+图向量）
│   │   ├── rag_retriever.py     # 混合检索+rerank+跨库
│   │   ├── vector_store.py      # LanceDB 向量存储（文本表+图片表+维度校验）
│   │   ├── rag_client.py        # embedding/rerank HTTP 客户端（重试+单图隔离+多格式）
│   │   ├── rag_enhance.py       # 查询改写+文档摘要
│   │   ├── knowledge_graph.py   # 知识图谱（实体+关系）
│   │   └── visualize.py         # 交互式 HTML 可视化
│   └── tests/                   # 测试 + fixtures
└── mcp/                         # 纯 stdio 适配层（薄入口，委托 Skill 层）
    ├── server.py                # FastMCP stdio：参数校验 + 调 Skill API + 输出
    ├── indexer.py               # DEPRECATED 旧 SQLite 索引器（server.py 不引用）
    ├── kbs.yaml                 # deprecated（向后兼容 fallback）
    ├── README.md
    ├── backends/                # 外部图文检索适配 / 共享重试 / 模型管理
    └── tests/
```

## 许可证

[MIT](LICENSE)

