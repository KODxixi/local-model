# AGENTS.md — local-model

> agent 第一入口：只存指针。规则全文在独立 .md。
>
> ⚠️ **调用前必读**：AGENTS.md 下方「模型配置速查」里有**两条否决性事实**
> （两类向量互斥 / 30B 单槽并发必须=1）—— 不读它们，一次正常调用就可能把别人的服务打停。
>
> **`README.md` 不是权威**：它给人看的项目介绍（29.8 KB），会复述本目录的数字，
> 允许滞后。两个文件冲突时，以本 AGENTS.md 的「真相源分配表」为准。
> 注意 README 里还有一批**表里没有行的事实**（「Agent 调用推荐配置」的
> max_tokens 16,384 / temperature 0.5、CUDA DLL 清单、DFlash `n_max=10`）——
> 那些是**对话场景的建议值，不是打标参数**，与 `retag_empty.py` 的 24576 不冲突；
> 但它们同样会漂，要改就补进真相源分配表。
> **路径约定**：以下命令假设你在 skill 根目录（AGENTS.md 所在目录）运行。
> 如果不在，先 cd 到 skill 根目录。

## 🚀 快速上手

**一句话**：把任意杂乱文件变成 agent 可命中的语义知识库。

```bash
# 0. 先看看有哪些知识库（doctor 输出里会列出所有 kb 和状态）
python scripts/cli.py doctor

python scripts/cli.py doctor

# 2. 建索引（增量：只处理变更文件）
python scripts/cli.py --kb <kb名> index

# 3. 检索（加 --explain 看为什么命中这个片段）
python scripts/cli.py --kb <kb名> retrieve "查询内容" --explain

# 4. 解析文件（PDF/图片/Word/PPT 等）
python scripts/cli.py ingest <文件路径> --json
```

> **首次使用**：先跑 `setup.ps1` 安装依赖，再跑 `doctor` 确认环境。

## 📋 任务速查（外部 agent 必读）

### 我想知道...
- **有哪些知识库？** → 跑 `python scripts/cli.py doctor`，输出里有所有 kb 和状态
- **索引新不新？** → 跑 `python scripts/cli.py --kb <kb> freshness`
- **向量库有多少条？** → 跑 `python scripts/cli.py --kb <kb> stats`
  - ⚠️ **例外：`archlib`（图文库）用 stats 量不到，恒返回 0。** 它的索引由 Archlib
    自己管理，local-rag 不重复建索引（`registry.local.yaml:36-46`）。要问答数走 Archlib CLI：
    `C:\GarchOS\archlib\.venv\Scripts\python.exe -m archlib --help`
    （子命令 `retrieve {build,search,vocab,verify}` / `doctor --no-write` / `tag`）。
    **2026-09-21 实测状态：`archlib retrieve verify` 报「索引与当前 facets.yaml hash 不兼容」**
    —— 索引本身已过期，拿真数字前得先解决这个；`doctor` 默认写盘，只读要用 `--no-write`。
- **检索结果为什么命中？** → 加 `--explain` 参数
- **文件怎么解析？** → 跑 `python scripts/cli.py ingest <文件> --json`
- **模型在线吗？** → **只读查**：`curl 127.0.0.1:9123/running`（已加载项 + `state` + ttl）
  + `curl 127.0.0.1:9123/v1/models`（含尚未加载的登记项）
  - ⚠️ **别用 `cli.py doctor` 当状态查询**：它会发一次真实 embedding 推理（`cli.py:588-600` 的冒烟测试，`timeout=10`），
    是**写操作级别**的动作 —— 会触发模型加载/驱逐。只读预检只用上面那两条 curl。

## ❌ 负面指针（不要去哪找）

- ❌ 不要去 `mcp/` 目录找 — 已删除，MCP 层已砍掉
- ❌ 不要去 `knowledge_graph.py` 找 — 已删除，kg 命令已砍掉
- ❌ 不要去 `visualize.py` 找 — 已删除，kg 可视化已砍掉
- ❌ 不要找 `test_retriever_innovations*.py` — 已删除，MMR/parent-child/多查询扩展已砍掉

## 模型配置速查

| 入口 | 模型 | 用途 | 红线 |
|---|---|---|---|
| 9123 | `text-embedding-*` / `vl-embedding-*` / `vl-reranker-*` | embed/rerank | 禁止把对话/VLM 模型塞进检索路径 |
| 9123 | `muse-glimmer-30b` | chat / VLM / 打标 | 禁止跑 embed/rerank |

**2026-09-21 甲-1 后端口不再是边界**：4 个模型全由 llama-swap 9123 托管，按**模型 id** 区分用途。
旧的 8080 独立实例已退役，计划任务 `MuseGlimmer` 置为 **Disabled** —— 重新启用会起第二个 30B，同卡装不下，直接 OOM。

**为什么不能混用**：embed/rerank 是向量计算，需要 GPU 全层 offload；chat/VLM 是生成式任务，两者显存需求不同。

### ⛔ 调用前必读的两条否决性事实（2026-09-21 独立审计补）

这两条**只写在这里**，因为它们决定"一次调用会不会把别人的服务打停"：

1. **两类向量互斥 —— 加载图向量会驱逐文本向量。**
   `text-embedding`(文本) 与 `vl-embedding-2b`(图文) 分属两个 `exclusive: true` 组，
   加载一个必然踹掉另一个。踢掉的代价是 **12.31s 冷加载**，而 OpenClaw 的
   `memory_search` 有**不可配的 30s 硬上限** —— 一次图检就能让全机文本检索进入冷加载窗口。
   **会触发的事**：`--kb <图文库>`、`search-image`、`verify-local-models.ps1 -Live`。
   （`--kb all` 已改为默认跳过 multimodal 库。）

2. **30B（`muse-glimmer-30b`）是单槽 —— 客户端并发必须 = 1。**
   多发请求**只会排队**，而排队时间**叠加**到客户端超时上（每条最多 90s）。
   从外部看就是"本地模型卡死了"，真实原因只是并发发多了。
   9123 侧同理：请求满了也排队，不报错 —— 排队会让你以为在跑，其实已在超时边缘。

> 具体模型名/维度/参数**不在本表** —— 见下面的「真相源分配表」。
> 2026-09-21 审计发现 13 个关键事实里有 12 个重复出现在 2–7 个文件，
> 每个副本都是一次漂移机会（本次一次性查出 11 处漂移）。数字只留一处。

> **归档提示**：`registry.local.yaml` 的改动前快照 —— 该文件是 gitignore 的，
> **快照是它旧状态的唯一记录** —— 已于 2026-09-21 移入 `D:\01_archive\backups\orphan-snapshots-20260921\`。
> 该目录的 `.archive-note.md` 写了来源、原因与「何时可以删」的判据。

## 真相源分配表（**改任何参数前先读这里**）

> 原则：**一个事实只有一个权威位置，其它文件只能指向它，不得复述数字。**
> 两个文件说同一件事时，以本表指定的那个为准。

> **本栈的总权威是一份跨项目文档：`C:\AI\memory\_canonical\LOCAL-MODELS.md`。**
> 它含两条轨的完整配置、显存账、消费方清单、硬规则、已否决项与未决项。
> 下表是**更细粒度**的分工（某些参数以代码/配置本身为准）；两者冲突时以代码/配置为准，
> 并**回头改 LOCAL-MODELS.md**（它是给人读的汇总，不是机器可执行的真相）。

| 事实 | 唯一真相源 | 为什么是它 |
|---|---|---|
| 30B 启动参数与生命周期（`-c` / `--parallel` / `--threads` / dflash / `ttl`） | `C:\AI\tools\llama-swap\config.yaml` 的 `muse-glimmer-30b` 条目 | **2026-09-21 甲-1**：30B 从 8080 独立进程迁入 llama-swap，全栈一个调度器。`ttl: 900` 即「自主卸载」 |
| 旧 8080 服务规范（**仅历史**） | `C:\AI\memory\_canonical\TOOLS.md` | 甲-1 后不再有独立 8080 实例；计划任务 `MuseGlimmer` 已 Disabled |
| 检索轨配置（模型 id / groups / ttl / `-c`） | `C:\AI\tools\llama-swap\config.yaml` | 配置即真相源 |
| 显存账与实测出处 | `C:\AI\memory\_canonical\LOCAL-MODELS.md` **第四节** | config.yaml 头部那段**现在只写「数字不在本文件」，一个数字都没有** —— 2026-09-21 一度有 6 个文档把读者指进那个空段落。以 LOCAL-MODELS.md 第四节为准 |
| 检索模型清单与端点 | `rules/registry.md` | 本 skill 内的模型台账 |
| 显存/并发不变量（破了会怎样） | `rules/redlines.md` | 红线只此一处 |
| 打标参数（max_tokens / 超时 / 并发 / 图片预处理） | `D:\Archlib_V2\runtime\retag_empty.py` | 生产代码即真相源 |
| 打标 prompt 正文 | `C:\GarchOS\archlib\system\tagging-prompt.md` | 只放 prompt，不放参数 |
| 架构决策与理由 | `rules/decisions.md` | |
| 性能数字、配置级陷阱长文 | `references/full-workflow.md` | 长文只有它能放下 |

**自检（改完跑一次）**：任何一条事实若在本表之外的 .md 里出现了**具体数字**，
就是重复真相源，应改为指针。

## 红线

- 4 个模型都走 9123；按**模型 id** 区分用途 —— embed/rerank 与对话/视觉不得混用同一个模型
- 索引是可再生投影，永不反向写
- 真相源：rules/redlines.md

## 常见错误速查

| 错误码 | 含义 | 怎么办 |
|---|---|---|
| `E_DEPENDENCY_MISSING` | 依赖未安装 | 跑 `setup.ps1` 安装依赖 |
| `E_CONNECTION_REFUSED` | 模型服务没启动 | 检查 9123（`schtasks /Query /TN LlamaSwap`），看 rules/troubleshooting.md |
| `E_UNKNOWN_KB` | 知识库名不对 | 先跑 `doctor` 列出所有可用 kb |
| `E_DIMENSION_MISMATCH` | 向量维度不匹配 | 换 embed 模型必须 `--force` 全量重建 |
| `E_INDEX_LOCKED` | 索引被另一进程锁定 | 等当前索引进程结束，或杀掉残留进程 |

## 命令

- 完整命令清单：tools/cli-commands.md
- 全局参数说明：tools/cli-commands.md#全局参数

## 配置

- registry.yaml 真相源：rules/registry.md
- 环境变量覆盖：rules/registry.md#配置分层

## 故障排查

- 常见错误与解决方案：rules/troubleshooting.md
- doctor 输出含义：rules/doctor-output.md

## Prompt 真相源

- PDF VLM：system/pdf-vlm-prompt.md
- 查询改写：system/query-rewrite-prompt.md
- 文档摘要：system/document-summary-prompt.md

## 开发

- 测试与发布流程：rules/dev-workflow.md
- 技术决策记录：rules/decisions.md

## 架构

- 分层架构说明：SKILL.md
- 完整工作流：references/full-workflow.md
