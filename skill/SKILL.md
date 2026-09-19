---
name: local-model
description: |
  本地 RAG 系统统一入口：文件解析（PDF/Word/Excel/PPT/图片）、智能分块、向量化索引、混合检索+重排。
  当需要解析杂乱文件、构建知识库、语义检索、召回相关文档时使用。
  检索模型走 llama-swap 9123，视觉/对话主模型走 Muse Glimmer 8080 (CUDA + DFlash + Vision)，向量存储用 LanceDB。
  不用于网页搜索、远程模型调用或未经确认的共享服务管理。
---

# 本地 RAG 系统（local-rag）

> **路径约定**：`<SKILL_ROOT>` = 本 skill 目录（公开仓库里就是 `<仓库根>/skill`），
> `<MODELS_DIR>` = GGUF 模型目录，`<LLAMA_CPP_DIR>` = llama.cpp 二进制目录。
> 完整安装/配置说明见仓库根的 `README.md`。

## 一句话

**把任意杂乱文件变成 agent 可命中的语义知识库。** 统一 CLI 入口 `scripts/cli.py`，一个命令搞定索引、检索、知识图谱、文件解析。

## 什么时候用

- 用户给了一堆文件（PDF/Word/Excel/PPT/图片/笔记），需要"能搜到内容"
- 需要在某个目录上建语义索引，然后自然语言查询
- 需要解析 PDF/图片中的文字（用 VLM，不需要单独的 OCR 引擎）
- 需要混合检索（语义 + 关键词）+ 重排精排
- 需要诊断本地模型和索引状态（`doctor` 命令一键检查）

## 首次使用：安装依赖

Skill 依赖 lancedb / pyarrow / PyMuPDF 等第三方库，首次使用前必须安装：

```powershell
# 在仓库根目录执行（脚本自己定位所在目录，不依赖 cwd）
powershell -ExecutionPolicy Bypass -File <SKILL_ROOT>\setup.ps1
```

安装完成后，所有命令使用 venv 中的 Python：

```powershell
# 方式1：激活 venv 后直接用 python
<SKILL_ROOT>\.venv\Scripts\Activate.ps1
python <SKILL_ROOT>\scripts\cli.py doctor

# 方式2：直接调用 venv Python（无需激活）
<SKILL_ROOT>\.venv\Scripts\python.exe <SKILL_ROOT>\scripts\cli.py doctor
```

**诊断依赖状态**：`cli.py doctor` 会最先检查依赖，缺失时明确提示哪些包没装。
跑测试还需补 `pip install pytest ruff`（`setup.ps1` 不装这两个）。

## 统一 CLI

所有功能通过一个入口调用：

```bash
python "<SKILL_ROOT>/scripts/cli.py" [全局参数] <子命令> [子命令参数]
```

**全局参数**：`--kb <name>`（**必填**）、`--db <path>`、`--registry <path>`、`--json`

> **⚠️ 全局参数位置（P0 陷阱）**：`--kb/--db/--registry` 是全局参数，**必须写在子命令之前**（与 `git`/`kubectl` 一致）。
>
> ✅ 正确：`cli.py --kb my_docs retrieve "query"`
> ❌ 错误：`cli.py retrieve "query" --kb my_docs` → 报 `unrecognized arguments: --kb`，CLI 会提示"全局参数请写在子命令之前"。

### 3 个核心动作

| 动作 | 命令 | 作用 |
|---|---|---|
| **建索引** | `cli.py --kb <name> index` | 扫描目录 → 解析 → 分块 → 向量化 → 写入 LanceDB（增量更新） |
| **检索** | `cli.py --kb <name> retrieve "<query>"` | 混合检索（语义+关键词）→ RRF 融合 → rerank 精排 → agent 友好格式 |
| **诊断** | `cli.py doctor` | 一键检查模型可用性、索引状态、知识库覆盖（不需要 `--kb`） |

### 完整命令清单

共 **15 个顶层子命令**（index / freshness / stats / retrieve / search-image / kg / ingest / chunk / rewrite / summary / embed / rerank / migrate / optimize / doctor）；其中 `kg` 含 5 个叶子子命令（extract/find/related/list/stats），**合计 19 个叶子命令**。所有子命令都支持 `--json`（默认人类可读文本）。

```bash
# 索引管理
cli.py --kb my_docs index                    # 增量建索引
cli.py --kb my_docs index --force            # 强制全量重建
cli.py --kb my_docs index --extract-entities # 同时提取实体到知识图谱
cli.py --kb my_docs freshness                # 检查索引是否过期（退出码 2=过期）
cli.py --kb my_docs stats                    # 查看 chunk 数、文件数

# 检索
cli.py --kb my_docs retrieve "查询" --top-k 8
cli.py --kb all retrieve "查询" --top-k 8            # 跨库检索（自动搜索所有已索引库）
cli.py --kb my_docs retrieve "query" --mode semantic # 仅语义
cli.py --kb my_docs retrieve "query" --mode keyword  # 仅关键词（最快，不调模型）
cli.py --kb my_docs retrieve "query" --json          # 输出 JSON
cli.py --kb my_docs search-image path/to/page.jpg    # 以图搜图（需要 multimodal 库）
cli.py --kb all retrieve "查询" --json               # 跨库检索 JSON（含 skipped 字段）

# 知识图谱
cli.py --kb my_docs kg extract <file>        # 从文件提取实体
cli.py --kb my_docs kg find "LanceDB"        # 找提到某实体的文档
cli.py --kb my_docs kg related "Python"      # 找共现实体
cli.py --kb my_docs kg list --type tech      # 列出实体（按类型过滤）
cli.py --kb my_docs kg stats                 # 图谱统计
cli.py --kb my_docs kg visualize out.html    # 生成交互式 HTML（需联网加载 vis-network）

# 文件解析（调试用，不需要 --kb）
cli.py ingest <file> --json                  # 解析任意文件 → 结构化 Document
cli.py chunk <file> --json                   # 智能分块（按标题/段落）

# 查询增强（不需要 --kb）
cli.py rewrite "复合查询"                    # LLM 改写查询（扩展同义词、拆解子查询）
cli.py summary <file>                        # 文档摘要 + 标签 + 关键实体

# 底层调试（不需要 --kb）
cli.py embed "text"                          # 生成 embedding
cli.py rerank "query" --docs doc1 doc2       # 精排

# 维护
cli.py --kb my_docs migrate --sqlite <path>  # 旧 SQLite 索引 → LanceDB
cli.py --kb my_docs optimize                 # 优化 LanceDB 表
cli.py doctor                                # 系统诊断（模型/GPU/索引/磁盘，--json 输出含 summary）
```

## 文件解析能力

| 类型 | 解析方式 | 说明 |
|---|---|---|
| 文本 (md/txt/py/json/yaml...) | 直接读取 | 自动识别编码，提取 markdown 标题 |
| PDF | PyMuPDF 文本层 + VLM | 有文本层直接提取；扫描件/图片页用 VLM 视觉理解 |
| Word (docx) | markitdown + python-docx | 提取文本、表格、元数据 |
| Excel (xlsx) | markitdown + openpyxl | 提取表格、sheet 元数据 |
| PPT (pptx) | markitdown + python-pptx | 提取文本、分页 |
| 图片 (png/jpg/webp...) | VLM 视觉理解 | OCR + 描述 + 分类，一次调用完成 |

**关键设计**：PDF 不依赖 PaddleOCR。PyMuPDF 提取文本层，文本不足的页面才调 VLM，VLM 同时做 OCR 和内容理解。

## 知识库注册表

`registry.yaml` 是唯一真相源。新增库 = 加一段配置，不改代码：

```yaml
knowledge_bases:
  my_docs:
    type: text                    # text / multimodal
    root: ~/Documents/my-kb
    patterns: ["*.pdf", "*.docx", "*.md"]
    dimensions: 4096
    embed_model: text-embedding-qwen3-embedding-8b
    capability:
      good_for: [文档全文检索]
      example_queries: [某份报告里的数据]
```

- **本机私有库写进 `registry.local.yaml`**（不进 git）：加载时它的 `knowledge_bases` 会合并到
  `registry.yaml` 之上，同名条目以本地文件为准。这样仓库里的注册表可以保持可公开。
- **端点可用环境变量覆盖**：`LOCAL_RAG_BASE_URL`（embed/rerank）、`LOCAL_RAG_VLM_BASE_URL`（VLM）、
  `LOCAL_RAG_LLM_BASE`（对话/改写）。
- 改完配置不需要动代码，直接跑 `cli.py doctor` 看是否注册成功。

## 与 MCP 的关系

**MCP (`local-models`) 做原子工具，Skill 做编排。**

| 层 | 位置 | 职责 |
|---|---|---|
| Skill (本目录) | `<SKILL_ROOT>` | 统一 CLI、文件解析、分块、索引编排、检索编排、agent 指南 |
| MCP | `<仓库根>/mcp` | 原子工具：embed/rerank/ocr/模型管理（供其他 agent 框架用） |

agent 优先用 Skill 的 `cli.py`（功能更强、接口统一），需要跨框架复用时用 MCP。

## 检索策略

默认 `mode="hybrid"`：

1. **查询侧增强（可选）**：`--context` 代词消歧（规则引擎，零延迟）、`--expand` LLM 同义改写；
   默认给查询加 Qwen 指令前缀 `Instruct: ...\nQuery: `（文档侧不变，不重建向量）
2. **语义召回**：query → embedding(4096 维) → LanceDB ANN → top N
3. **关键词召回**：query → LanceDB FTS（**BM25**，`base_tokenizer=ngram` 2–4 字，中文友好）→ top N
   - `--path-filter` 用服务端预过滤在排名前生效，目标排到 1000+ 也能命中
4. **RRF 融合**：语义/关键词各自排名后按 Reciprocal Rank Fusion 合并（不直接混原始分数）
5. **稳定去重**：`chunk_id` 优先，缺失用 path + 完整内容 sha256 前 16 位
6. **rerank 精排**：`text-reranker-8b`（cross-encoder），候选数默认 24
7. 可选 `--mmr` 多样性重排、`--parent-child` 扩展父文档上下文、`--auto-route` 自动选模式

可选模式：`semantic`（仅语义）、`keyword`（仅关键词，不调模型，最快）、`hybrid`（默认）。

补充：知识图谱 `kg find/related` 走实体关联，补向量检索的"语义相似"盲区。

## 架构

```
用户文件 (PDF/Word/Excel/PPT/图片/文本)
    │
    ▼
cli.py ingest ──→ Document (结构化: 元数据 + 全文 + 分页)
    │              │
    │              ▼
    │         chunker ──→ Chunk (按标题/段落分块, 非固定字符数)
    │              │
    ▼              ▼
cli.py index ──→ embed (llama-swap 9123) ──→ LanceDB (ANN + FTS)
    │
    ▼
cli.py retrieve ──→ 混合召回 (语义+关键词) ──→ RRF ──→ rerank ──→ agent 友好格式
    │
    └──→ kg extract/find/related ──→ 知识图谱 (实体-文档关系)
```

## 显存与优先级（铁律，2026-09-20 立）

RTX 5090 D 共 32.6 GB。实测：**30B 常驻约 22 GB**，两个 8B 检索模型（embedding +
reranker，Q4_K_M + KV）约 10 GB —— **同时在场必然超订**：
实测占用 31.3 / 32.6 GB（96%），rerank 直接 `TimeoutError`（`DEFAULT_TIMEOUT=180s`
× 5 次重试 ≈ 15 分钟），对上层就是"检索不可用"。

1. **一切本地模型按需调用，不常驻。** 检索模型靠 llama-swap 的 TTL 自动装卸；
   30B **没有 TTL、是手工起的 llama-server**，必须显式启停。
2. **优先级：向量/检索模型 > 30B。** 冲突时让 30B 让路，绝不反过来压缩检索。
3. **30B 只在打标 / 批量视觉任务时按需起，用完停掉。** 它不是常驻服务。
4. **遇到"两个都要在场"的显存冲突**：不要自行调度、不要降级硬跑 ——
   **立即通知用户**（说明谁在占显存、要起什么、预计占用），由用户决定启停顺序。

30B 启动命令见本文末「Muse Glimmer 30B + DFlash + Vision」一节；
停止 = 结束该 llama-server 进程（无对应计划任务）。

## 安全与边界

- 索引是**可再生投影**，真相源是原始文件；永不反向写
- `index` 会写入 LanceDB（`~/.local-rag/lancedb`），属于本地操作
- 视觉理解调用 8080 的 Vision 能力，可能触发模型加载
- 启停/重启 llama-swap、对话模型服务等共享服务需要用户明确授权
- 不读取、输出或改写凭据；不扫描 private 目录

## GPU 使用

所有本地模型默认跑在 GPU 上：

| 服务 | 后端 | GPU 层 | 说明 |
|---|---|---|---|
| llama-swap 9123 | Vulkan llama.cpp | `-ngl 99`（全层） | 文本/图文 embedding + rerank，4 个模型，TTL 自动装卸 |
| Muse Glimmer 8080 | CUDA + DFlash + Vision | `--gpu-layers 99` | 对话 / Agent / 视觉主模型，125–220 tok/s |

### Muse Glimmer 30B + DFlash + Vision（对话 / 视觉主模型）

**两种已验证配置（RTX 5090 D 32GB）：**

| 场景 | `-c` | `--parallel` | `--spec-draft-n-max` |
|---|---|---|---|
| 批量视觉打标 | 32768 | 8 | 10 |
| 对话 / Agent（长上下文） | 131072 | 1 | 10 |

```powershell
# 先把这两个变量换成你自己的路径
$LlamaDir = "<LLAMA_CPP_DIR>"       # 例：C:\tools\llama.cpp-cuda
$MuseDir  = "<MODELS_DIR>\Muse"     # 例：C:\models\Muse

# 批量打标用 -c 32768 --parallel 8；对话场景改 -c 131072、去掉 --parallel
& "$LlamaDir\llama-server.exe" `
    --gpu-layers 99 `
    -m "$MuseDir\Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf" `
    --mmproj "$MuseDir\mmproj-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-type draft-dflash `
    --spec-draft-model "$MuseDir\dflash-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-draft-n-max 10 `
    -fa on -c 32768 --threads 20 --parallel 8 `
    --port 8080 --host 127.0.0.1
```

**关键参数说明：**
- `--gpu-layers 99`：全层 offload 到 GPU（不设就退回 CPU，慢一个数量级）
- `--mmproj`：多模态投影器，**必须加**否则 Vision 不生效
- `--spec-type draft-dflash` + `--spec-draft-model`：DFlash 投机解码
- `--spec-draft-n-max 10`：草稿长度（训练 block size=16，15 偏高、更慢更不稳）
- `-fa on`：Flash Attention；`-c 131072`：128K 上下文（原生最大，扩到 256K 会爆显存）

**CUDA 环境要求：**
- CUDA 13.3 运行时（DLL 在 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\x64\`）
- 以下 DLL 必须复制到 llama.cpp 目录，否则找不到 GPU：
  `cudart64_13.dll` / `cublas64_13.dll` / `cublasLt64_13.dll` / `nvblas64_13.dll`

**批量视觉打标参数（实测）：**

| 参数 | 值 | 理由 |
|---|---|---|
| max_tokens | 8192 | 推理模型的 `reasoning_content` 吃掉大量 token，<4096 会导致 `content` 为空 |
| temperature | 0.3 | 标签任务要准确，不要创造性发挥 |
| 图片格式 | JPEG 768px q80 | llama.cpp 不支持 webp data URI，运行时转码；768px 比 1024px 快 47% |
| 并发 | 8 线程 | GPU 利用率约 85% 已饱和，再高只排队 |

**速度指标（8 并发实测）：** 单张 4–22s（分析图快、效果图慢），吞吐 15–20 张/分，
显存 23–24 GB / 32.6 GB，功耗约 437W。

**已知坑（实测）：**
- `--parallel >8`（16/32/64）触发 HTTP 400 Bad Request——llama.cpp 多 slot 配置 bug，**8 是稳定极限**
- 64 线程同时发请求会打爆 HTTP server（并发约 10MB base64），必须控制在 8 个在途
- 推理模型返回 `reasoning_content`（思考）和 `content`（正式回答），**只取 `content`**
- `max_tokens < 4096` 时 reasoning 吃光导致 content 为空，必须 ≥8192

## 已知限制

- 图片/扫描页入库慢（每张走一次 VLM），大量图片建索引耗时长
- 单文件默认上限 5MB、单库默认上限 10000 文件（`registry.yaml` 可改）；
  文件数超上限时孤儿清理自动停用，避免误删未扫描到的文件
- 维度不可原地修改：换 `embed_model` 必须 `--force` 全量重建
- `kg visualize` 生成的页面从 CDN 加载 vis-network，离线时图谱不渲染（页面会提示）

## 验收

- `cli.py doctor` — 模型在线、索引存在、知识库有覆盖
- `cli.py --kb <name> stats` — 查看 chunk 数和文件数
- `cli.py --kb <name> retrieve "<已知内容>"` — 验证 top-3 命中率
- 修改一个文件后 `cli.py index` — 验证只处理变更文件（增量）
- 删除一个文件后 `cli.py index` — 验证其 chunks 被清理（孤儿 prune）
