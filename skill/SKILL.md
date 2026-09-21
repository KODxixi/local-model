---
name: local-model
description: |
  本地 RAG 系统统一入口：文件解析（PDF/Word/Excel/PPT/图片）、智能分块、向量化索引、混合检索+重排。
  当需要解析杂乱文件、构建知识库、语义检索、召回相关文档时使用。
  4 个模型（检索 3 + 对话/视觉 1）全由 llama-swap 9123 托管（CUDA + DFlash + Vision），向量存储用 LanceDB。
  不用于网页搜索、远程模型调用或未经确认的共享服务管理。
---

# 本地 RAG 系统（local-rag）

> **路径约定**：`<SKILL_ROOT>` = 本 skill 目录（公开仓库里就是 `<仓库根>/skill`），
> `<MODELS_DIR>` = GGUF 模型目录，`<LLAMA_CPP_DIR>` = llama.cpp 二进制目录。
> 完整安装/配置说明见仓库根的 `README.md`。

## ⚠️ 先读这个：本 skill 的真相源在哪里

> **本文件是运行时入口，但它不是所有事实的真相源。** 改任何参数前先看
> `AGENTS.md` 的「**真相源分配表**」—— 那里指定了每类事实的唯一权威位置：
>
> | 事实 | 唯一真相源 |
> |---|---|
> | 30B 启动参数（`-c` / `--parallel` / `--alias` / `ttl`） | `C:\AI\tools\llama-swap\config.yaml` |
> | 检索轨配置 | `C:\AI\tools\llama-swap\config.yaml` |
> | 显存账 | `C:\AI\memory\_canonical\LOCAL-MODELS.md` **第四节** |
> | 打标参数（max_tokens / 超时 / 并发） | `D:\Archlib_V2\runtime\retag_empty.py` |
> | 检索模型清单 | `rules/registry.md` |
> | 显存/并发不变量 | `rules/redlines.md` |
>
> **本文件里出现的数字都是镜像**（为让人不必跳文件就能当场判断），冲突时以上表为准。
> 2026-09-21 的独立审计发现：本文件曾与真相源冲突 3 处（超时 30s vs 90s、
> 余量 ~900 vs 1353、同步三处 vs 任务无需改）—— **镜像会漂，真相源不会。**

## 一句话

**把任意杂乱文件变成 agent 可命中的语义知识库。** 统一 CLI 入口 `scripts/cli.py`，一个命令搞定索引、检索、文件解析。

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

共 **12 个顶层子命令**（`index` / `freshness` / `stats` / `retrieve` / `search-image` / `ingest` / `chunk` / `rewrite` / `summary` / `embed` / `rerank` / `doctor`），**没有叶子子命令**。全部支持 `--json`（默认人类可读文本）。

> ⚠️ **2026-09-21 更正**：本文档此前写「15 个顶层子命令、19 个叶子」，并教 `kg *` / `migrate` / `optimize`
> 与 `--mmr` / `--expand` / `--auto-route` / `--parent-child` —— **这些在 CLI 里都不存在**，
> 照它跑必然 `unrecognized arguments`。上面这 12 个是 `cli.py --help` 实测输出。
> 另有两个**存在但过去从未被文档提过**的 flag：`--explain`（显示每条的语义/关键词/RRF/rerank 分数）
> 与 `--trace out.html`（生成检索推理链可视化）。

```bash
# 索引管理
cli.py --kb my_docs index                    # 增量建索引
cli.py --kb my_docs index --force            # 强制全量重建
cli.py --kb my_docs index --extract-entities # 同时抽实体入库（注意：**读取端已砍掉**，只写不读）
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

# 查询诊断（替代了已砍掉的知识图谱读取命令）
cli.py --kb my_docs retrieve "查询" --explain         # 显示每条的四路分数（语义/关键词/RRF/rerank）
cli.py --kb my_docs retrieve "查询" --trace out.html  # 生成检索推理链可视化
cli.py --kb my_docs retrieve "查询" --no-rerank        # 跳过精排，只看召回

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
cli.py --kb my_docs index --force            # 全量重建（等价于重建索引）
cli.py --kb my_docs index --no-prune         # 保留孤儿 chunks（默认会清）
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

## 与 MCP 的关系（已终止）

> ⚠️ **`local-models` MCP 已于 2026-09-21 退役**：目录已删，`servers.json` 移入 `retired` 段，
> 入口收敛到本 skill 的 `scripts/cli.py`。**本 skill 是唯一边界**——不存在"另一层原子工具"。
>
> 半退役的症状是启动即 `local-models (CONNECTION_CLOSED)`（注册表还在、入口已删）。
> 若将来要重建 MCP 层，先读 `C:\AI\mcp\README.md` 的退役规则（必须同清四处）。

## 检索策略

默认 `mode="hybrid"`：

1. **查询侧增强**：`--context` 在 **`rewrite` 子命令**上（对话历史/知识库描述，规则引擎零延迟）；
   ⚠️ **`retrieve` 没有 `--context`**（2026-09-21 实测 `cli.py retrieve --help`）—— 代词查询请把代词换成实体名再检索；
   默认给查询加 Qwen 指令前缀 `Instruct: ...\nQuery: `（文档侧不变，不重建向量）
2. **语义召回**：query → embedding(4096 维) → LanceDB ANN → top N
3. **关键词召回**：query → LanceDB FTS（**BM25**，`base_tokenizer=ngram` 2–4 字，中文友好）→ top N
   - `--path-filter` 用服务端预过滤在排名前生效，目标排到 1000+ 也能命中
4. **RRF 融合**：语义/关键词各自排名后按 Reciprocal Rank Fusion 合并（不直接混原始分数）
5. **稳定去重**：`chunk_id` 优先，缺失用 path + 完整内容 sha256 前 16 位
6. **rerank 精排**：`vl-reranker-2b`（cross-encoder，文本+图文共用），候选数默认 24
7. 可选 `--no-rerank`（跳过精排看纯召回）、`--explain`（看每条的四路分数）、`--trace out.html`（推理链可视化）、`--path-filter`（服务端预过滤）

可选模式：`semantic`（仅语义）、`keyword`（仅关键词，不调模型，最快）、`hybrid`（默认）。


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
```

## 显存与优先级（铁律，2026-09-21 实测重写）

> **本节不写数字。** 唯一权威：`C:\AI\memory\_canonical\LOCAL-MODELS.md` 第四节
> （给的是**场景 + 区间**，不是孤立数字 —— 整卡读数有 ±50–300 MiB 抖动，单点值必漂）。
> 本节只保留判断「能不能再加载一个模型」所需的结构性结论。
> 改配置后请以 LOCAL-MODELS.md 第四节为准 —— 本节只是让人不必跳文件就能判断能不能加载模型。
> 2026-09-21 之前的版本是估算（"30B ~22GB + 检索 ~10GB"），**低估检索栈近一倍**，
> 导致"必然超订"的错误结论，以及多轮无效的 ttl/preload 调整。

RTX 5090 D 共 32.6 GB（32607 MiB）。实测：

| 项 | 实测 |
|---|---|
| 桌面/系统基线 | 1.8 GB |
| Muse 30B 常驻（权重 16.76 + mmproj 1.40 + dflash 1.63 + KV，**单槽**） | **20.0 GB** |
| text-embedding-8b @`-c 4096` | **6.57 GB** |
| vl-reranker-2b @`-c 8192` | **1.36 GB** |
| vl-embedding-2b @`-c 4096` | **4.40 GB** |

**结论：检索栈已能与 30B 同时常驻**（这是 2026-09-21 修复后才成立的）：

```
本次配置下存在三个场景（数字见下方针尖指向的权威段，本文件不复述）：
  A 只 30B            —— 检索栈全卸
  B 文本栈 + 30B       —— 常驻稳态（OpenClaw 记忆 / ChatOS 索引 / Fang-data 用）
  C 图文栈 + 30B       —— archlib 图检时
  D 三模型共驻         —— **不可达**：两个 embed 组 exclusive 互斥
```

> ⚠️ **数字的唯一权威是 `C:\AI\memory\_canonical\LOCAL-MODELS.md` 第四节。**
> 它给的是**场景 + 区间**（整卡读数有 ±50–300 MiB 抖动，单点值必漂）。
> 本文件、rules/、TOOLS.md 一律只指路、不复述 —— 有多处复述正是此前反复漂移的成因。

修复前的状态是**超订 9.1 GB**（30B + 文本对 = 40.0 GB）。根因两条，都不是"模型太多"：
1. **`-c` 严重超配** —— text-embedding 开 16384（消费方最多用 2048）、
   text-reranker 开 32768，两者白占约 9.4 GB KV；
2. **`multimodal` 组的 `swap: true` 用反了** —— 把 vl-embedding 与 vl-reranker 设成互斥，
   而图文检索正是 embed→rerank 两步，导致**每查一次装卸一个 6 GB 模型**。

1. **文本向量 + 共用 reranker 常驻**（`ttl: 0`）。**理由要分清楚**：OpenClaw 的
   `memory_search` 硬上限是 **30s**（`DEFAULT_MEMORY_SEARCH_TIMEOUT_MS = 3e4`，不可配；
   2026-09-21 从已装 openclaw@2026.9.5 的 `dist/tools-*.mjs` 核实，旧文档写的 15s 已作废），
   而本栈冷加载实测：text-embedding 4.29s、vl-reranker-2b **16.27s**（全栈最慢）。
   **16.27s < 30s，所以常驻不是为了「避免超时」，是为了不让交互检索进退冷加载。**
   要省这 6.57+1.36 GB 可以改 `ttl: 300`，代价是空闲 5 分钟后的首次检索 +4.3s（reranker 更久）。
   ⚠️ 别再复述成「15s 会超时所以必须常驻」—— 数字已变，理由也变了。
2. **两类向量互斥**（`text-retrieval` 与 `visual-retrieval` 各自 `exclusive: true`）。
   理由：文本 embed 6.57 + 图文 embed 4.40 + rerank 1.36 = 12.3 GB，超过检索侧可用上限
   （检索侧上限 ≈8.6 GB）；而文本检索与图文检索天然不同时发生，所以互斥是零代价的。
3. **reranker 组必须 `persistent: true`** —— 文本与图文检索都要串用它，绝不能被别组驱逐。
   实测已验：加载 text-embedding 不会驱逐它；加载 vl-embedding 会驱逐 text-embedding
   但**不会**驱逐它。
4. **常驻稳态余量很薄**（具体数字见 LOCAL-MODELS.md 第四节，本文件不复述）。
   这张卡不能再同时跑别的吃显存的程序（游戏 / 浏览器 GPU 加速 /
   第二个 CUDA 应用）。真撞 OOM 时的零风险杠杆只有两个：
   text-embedding 的 `-c` 4096→2048（省约 0.3 GB），或 `-b/-ub` 2048→1024
   （省约 0.5 GB，代价是建索引吞吐）。**不要动 30B 的 dflash / mmproj。**
5. **30B 由 llama-swap 托管**（2026-09-21 甲-1）：参数写在
   `C:\AI\tools\llama-swap\config.yaml` 的 `muse-glimmer-30b` 条目，
   `ttl: 900` 空闲自卸 —— 这就是「自主卸载」。**改参数须同步 skill 文档**。
   旧计划任务 `MuseGlimmer` 已 **Disabled**（启用会双开第二个 30B → OOM）。
   —— 参数与文档不同步正是 2026-09 那轮"反复改坏"的成因。
6. **遇到显存冲突**（OOM、或要临时加载未登记的模型）：不要自行调度、不要降级硬跑 ——
   **立即通知用户**，说明谁在占显存、要起什么、预计占用，由用户决定启停顺序。

## 安全与边界

- 索引是**可再生投影**，真相源是原始文件；永不反向写
- `index` 会写入 LanceDB（`~/.local-rag/lancedb`），属于本地操作
- 视觉理解走 9123 的 `muse-glimmer-30b`（Vision），可能触发冷加载（实测 ~54s）
- 启停/重启 llama-swap、对话模型服务等共享服务需要用户明确授权
- 不读取、输出或改写凭据；不扫描 private 目录

## GPU 使用

所有本地模型默认跑在 GPU 上：

| 服务 | 后端 | GPU 层 | 说明 |
|---|---|---|---|
| llama-swap 9123 | **CUDA llama.cpp**（2026-09-21 由 Vulkan 切换） | `--gpu-layers 99`，`-b4096 -ub4096 -fa on` | 文本/图文 embedding + rerank，**3 个模型**（text-reranker-8b 已移除），TTL 自动装卸。**不要加 `--parallel`** |
| llama-swap 9123 | CUDA + DFlash + Vision | `--gpu-layers 99` | 对话 / Agent / 视觉主模型 `muse-glimmer-30b`，125–220 tok/s，`ttl: 900` 空闲自卸（2026-09-21 甲-1 迁入；此前是 8080 独立进程） |
> **2026-09-21 后端切换**：检索三模型（text-embed / vl-embed / vl-rerank）从 `llama.cpp-vulkan` 改 `llama.cpp-cuda`。embedding 是批量 prefill（矩阵-矩阵），Vulkan 在 NVIDIA 拿不到 Tensor Core，实测 ~17/s；CUDA 稳态 ~140–150/s（无 500 断崖）。详见 ChatOS rules/vector-index.md。
> ⚠️ **显存数字见 `C:\AI\memory\_canonical\LOCAL-MODELS.md` 第四节（场景 + 区间）—— 本文件不复述。**
> **不存在「三模型共驻」态** —— 两个 embed 组互斥，任何「~31.7GB 三模型共驻」的说法都测不出来。

### Muse Glimmer 30B + DFlash + Vision（对话 / 视觉主模型）

**已验证配置（RTX 5090 D 32GB，2026-09-21 起唯一一份）：**

| `-c` | `--parallel` | 每槽上下文 | `--spec-draft-n-max` |
|---|---|---|---|
| 32768 | **1** | **32768** | 10 |

> **每槽上下文 = `-c ÷ --parallel`** —— 这是踩过的坑。旧配置是 `-c 32768 --parallel 8`，
> 每槽只有 4096，而打标要 `max_tokens=8192`，**永远放不下**；只因 `content` 短（~300 字）
> 才没暴露。同理 `--parallel > 8` 会触发 llama.cpp 多 slot 的 HTTP 400。
> ⚠️ **改 `--parallel` 会改变显存**（曾误写为「池守恒、零增量」）。本机实测同一 `-c 32768`：
> `--parallel 8` → 21,573 MiB，`--parallel 2` → 20,652.5 MiB，**`--parallel 1` → 20,432 MiB**。
> 原因：Muse Glimmer 39/52 层是**滑窗注意力**（`sliding_window=2048`，pattern `[T,T,T,F]×13`），
> 而 `--swa-full` 默认 false —— SWA 缓存随序列数变化。所以 `-c` 不是唯一变量。
> 换言之：**`--parallel 8 → 1` 共省 1,141 MiB**，直接变成检索引擎的余量（Vulkan 时代 902 → 1,353 MiB；
> 换 CUDA 后开销变大 —— 数字一律以 LOCAL-MODELS.md 第四节为准）。
> **`--parallel 1`（2026-09-21 定）**：取「慢但稳」。单槽没有多槽争抢 KV/批处理，
> 彻底绕开 llama.cpp 多 slot 的 400 bug，SWA 缓存也随序列数下降。
> **代价：所有客户端必须并发=1** —— 多发的请求只会排队，
> 而排队时间会叠加到客户端超时上（打标脚本超时 **90s**，p99 16.4s×2 就已逼近）。

```powershell
# 2026-09-21 甲-1：30B 由 llama-swap 托管，**不要手工起独立实例**。
# 首个请求按 config.yaml 的 muse-glimmer-30b 条目自动 spawn；ttl: 900 空闲自卸。
# 改参数 = 改 config.yaml，然后 schtasks /End /TN LlamaSwap; schtasks /Run /TN LlamaSwap

# 前台调试命令（参数与 config.yaml 一致；$LlamaDir / $MuseDir 换成你自己的路径）
# ⚠️ 端口必须避开 8080（旧独立实例）与 9123（llama-swap），否则会起出第二个 30B → OOM
& "$LlamaDir\llama-server.exe" `
    --gpu-layers 99 `
    -m "$MuseDir\Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf" `
    --mmproj "$MuseDir\mmproj-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-type draft-dflash `
    --spec-draft-model "$MuseDir\dflash-Muse-Glimmer-30B-Q4_K_M.gguf" `
    --spec-draft-n-max 10 `
    -fa on -c 16384 --threads 20 --parallel 1 `
    --port 8099 --host 127.0.0.1
```

**关键参数说明：**
- `--gpu-layers 99`：全层 offload 到 GPU（不设就退回 CPU，慢一个数量级）
- `--mmproj`：多模态投影器，**必须加**否则 Vision 不生效
- `--spec-type draft-dflash` + `--spec-draft-model`：DFlash 投机解码
- `--spec-draft-n-max 10`：草稿长度（训练 block size=16，15 偏高、更慢更不稳）
- `-fa on`：Flash Attention；`-c 32768`：总上下文池（`--parallel 1` → 单槽即 32768）。
  模型 `n_ctx_train` 是 131072，但**128K 全开 + 检索栈共存会超订**，故用 32768
- `--alias muse-glimmer-30b`：**必须加**。llama-server 默认让 `/v1/models` 返回 GGUF 全路径，
  而 `chatos-console` 会拿 `model.id === LOCAL_LLM_MODEL` 做校验 → 不加就直接 503
  `local_model_not_loaded`。（`/v1/chat/completions` 本身忽略 model 字段，所以少了它
  只有"校验类"调用方会挂，症状隐蔽。）

**CUDA 环境要求：**
- CUDA 13.3 运行时（DLL 在 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3\bin\x64\`）
- 以下 DLL 必须复制到 llama.cpp 目录，否则找不到 GPU：
  `cudart64_13.dll` / `cublas64_13.dll` / `cublasLt64_13.dll` / `nvblas64_13.dll`

**批量视觉打标参数（以生产脚本为准）：**

> **真相源是生产脚本 `D:\Archlib_V2\runtime\retag_empty.py`，不是本表。**
> 下表是 2026-09-21 从该脚本核出来的。此前本表记的是另一套（8192 / temp 0.3 / 8 线程），
> **四行全错** —— 照它调参会调出与生产不同的东西。

| 参数 | 生产真值 | 位置 |
|---|---|---|
| max_tokens | **24576（视觉类）/ 12288（分析类预分流）** | `retag_empty.py:186` |
| temperature | **0.1** | `retag_empty.py:196` |
| reasoning_effort | `low` | `retag_empty.py:198` |
| 图片格式 | JPEG **768px q80**（并设 `Image.MAX_IMAGE_PIXELS=None` 以吃下 112MP 总图） | `retag_empty.py:20,177` |
| 并发 | **1**（`ThreadPoolExecutor(max_workers=1)`，**必须 = 30B 的 `--parallel`**）；失败重试 1 线程 | `retag_empty.py:300,351` |
| 客户端超时 | **90s**（与 max_tokens 同抬）；**只有 429 会重试**，500/503/连接拒绝直接进失败清单 | `retag_empty.py:214` |

> **max_tokens 与客户端超时必须成对调整**，这是容易漏的一条：实测生成 ~116–130 tok/s，
> 超时 30s 只能出 ~3,900 token —— **高于它的 max_tokens 额度永远走不到，等于白设**。
> 90s ↔ ~11,700 token。要真把 24576 用满，超时需 ~190s（不推荐：workers=1 时一次卡住就堵 190s）。
>
> ⚠️ **2026-09-21 甲-1 之后还有第二道上限：上下文。** 30B 迁入 llama-swap 时 `-c` 从
> 32768 降到 **16384**（与检索栈共存所需）。llama-server 对超限的 `max_tokens`
> **不报错、静默钳位**（实测 HTTP 200），所以 `prompt(~2.9k) + 24576` 实际只能出 ~13.5k。
> 仍远高于实测 ~750 token 的产出，**打标不受影响**；但 `-c` 再降就会真截断。
> 抬 `-c` 的代价是与检索栈争显存 —— 动作前先读 LOCAL-MODELS.md 第四节。
> （`retag_empty.py:182` 的注释仍写着「单槽 32768」的旧算术，已过期。）

**速度指标**（2026-09-21 从 `_retag_v10.log` 的 **15,216 条成功记录**统计，8080 CUDA 实例）：
p50 **7.7s** / p90 10.1s / p99 16.4s / max 41.8s / mean 8.0s。

> ⚠️ **别再用旧数字。** 本表此前写「单张 4–22s、吞吐 15–20 张/分、8 并发」。
> 那些出自 `_archive/_ab_test_full.py` 的对照实验，而且**跑在 9123 的 Vulkan 副本上**
> （生产是 8080 CUDA），参数也不同（长 prompt / 无 system / temp 0.3 / 8192）。
> 同实验 phase2 实测 8 并发只有 **11.9 张/分**（4 并发 10.6，1 并发 1.7），
> 同样没复现 15–20。**8080 上的真实并发吞吐至今未测** —— 要用就自己测一遍。
> 另有一处未定论：客户端超时写 30s，而日志里成功条目 max 41.8s，二者矛盾
> （脚本 2026-09-21 改过），无法从只读侧判断。

**已知坑（实测）：**
- `--parallel >8`（16/32/64）触发 HTTP 400 Bad Request——llama.cpp 多 slot 配置 bug，**8 是稳定极限**
- 64 线程同时发请求会打爆 HTTP server（并发约 10MB base64），必须按 workers 控制在途数
- 推理模型返回 `reasoning_content`（思考）和 `content`（正式回答），**只取 `content`**
- **`max_tokens` 给小了会让 `content` 为空**：模型先烧 reasoning 再出 content。
  实测一次 trivial 请求的 reasoning 就到 ~680 token（`reasoning_effort=low` 时降到 ~150）。
  打标这类长 prompt 必须 ≥8192。
  ⚠️ **这个失败是静默的**：空 `content` **不抛异常**，调用方若"异常→兜底"的逻辑会被绕过
  （ChatOS 就踩过：本地返回空串、既不报错也不回退云端，直接给出空摘要）。
  给消费方配 `max_tokens` 时按 ≥4096 起步，并同时给本地路径加 `reasoning_effort: low`。

## 已知限制

- 图片/扫描页入库慢（每张走一次 VLM），大量图片建索引耗时长
- 单文件默认上限 5MB、单库默认上限 10000 文件（`registry.yaml` 可改）；
  文件数超上限时孤儿清理自动停用，避免误删未扫描到的文件
- 维度不可原地修改：换 `embed_model` 必须 `--force` 全量重建

## 验收

- `cli.py doctor` — 模型在线、索引存在、知识库有覆盖
- `cli.py --kb <name> stats` — 查看 chunk 数和文件数
- `cli.py --kb <name> retrieve "<已知内容>"` — 验证 top-3 命中率
- 修改一个文件后 `cli.py index` — 验证只处理变更文件（增量）
- 删除一个文件后 `cli.py index` — 验证其 chunks 被清理（孤儿 prune）
