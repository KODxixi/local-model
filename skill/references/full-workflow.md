---
name: local-model
description: 本地模型统一调用与调度（llama-swap + 对话端点）。底层实现参考：端点、模型、配置陷阱、排障手册、性能测量方法。agent 日常调用请用 scripts/cli.py，用法见 SKILL.md。
---

# 本地模型底层实现参考

> **文档定位**：本文是**底层实现参考**（端点、模型、配置陷阱、排障手册），供调试和改配置时查阅。
> **agent 日常调用请用 `scripts/cli.py` 统一入口**，用法见 `SKILL.md`；安装与项目总览见仓库根 `README.md`。

## 操作边界

- 只读查询（`list_kbs` / `list_models`）随时可跑；`status` 会执行真实的 embedding/rerank 健康推理，
  可能触发按需加载，须在模型调用授权范围内。
- 默认 `search` / `rerank` / `embed` 触发推理但不管理服务；`index` 会重建可再生投影（写入）。
- `load_model` / `unload_model` 会改变显存占用和当前已加载模型；仅在用户要求模型调度或任务确实需要时执行，
  执行前说明影响。
- **启停或重启 llama-swap、对话模型服务等共享服务属于服务管理，需要用户在当前轮次明确授权。**
  模型装卸不等于服务管理。

## 核心原则

1. **统一入口**：本地推理和模型调度走统一接口，不手写 embedding/rerank 调用、不手工拼 llama-server 参数。
   只读诊断例外：可对已登记的 loopback 端点执行 `GET /running` 或查看 `/v1/models` 注册列表；
   注册不等于已加载，这不授权 POST、推理、装卸或服务管理。
2. **检索 vs 对话双轨**：检索模型（embed/rerank）由 **llama-swap**（端口 9123）统一管理——
   按需 spawn、TTL 自动卸载、单 OpenAI 兼容端点；对话/生成走另一路（默认 8080，可环境变量覆盖）。
3. **配置驱动**：新增知识库 = 改 `registry.yaml` 加一段；改模型 = 改 llama-swap 的 `config.yaml`，不改代码。

### 红线

1. **embed/rerank 永远只走检索端点（9123）**；对话/视觉端点（默认 8080）只跑对话/识图。
2. **禁止**把 `text-embedding-*` / `*-reranker-*` load 进对话端点；
   `load_model` / `unload_model` / `list_models` 只管对话与视觉模型。
3. **禁止**把任何 embed/rerank 端点配置指向对话/视觉端点（默认 8080）。检索不通时先 `curl 127.0.0.1:9123/running`
   （模型 TTL 装卸，看当前加载），**绝不**靠改端点到别处应急。

## 架构

```
llama-swap (127.0.0.1:9123, 单端点 /v1/embeddings + /v1/rerank + /v1/models)
 ├─ text-embedding-qwen3-embedding-8b   Qwen3-Embedding-8B Q4_K_M     (Vulkan llama.cpp)
 ├─ text-reranker-8b                    Qwen3-Reranker-8B Q4_K_M
 ├─ vl-embedding-2b                     Qwen3-VL-Embedding-2B Q8_0 (+mmproj)
 └─ vl-reranker-2b                      Qwen3-VL-Reranker-2B 自转 Q8_0
对话/视觉端点 (默认 8080)   ← 仅对话/识图（VLM）
外部图文检索 CLI  ← LanceDB 检索（embed/rerank 走 llama-swap）
```

配置：llama-swap 的 `config.yaml`；推理运行时目录下放 llama-server 二进制（Vulkan 版走 GPU）。

## 工具接口（MCP 层）

| 工具 | 签名 | 用途 |
|---|---|---|
| `list_kbs` | `()` | 列出知识库、类型、后端与声明能力；不代表实时覆盖或推理已验收 |
| `search` | `(kb, query="", top_k=8, image=None, mode="hybrid")` | 按 kb 路由；图文库可显式 `mode="keyword"` 快速检索 |
| `index` | `(kb)` | 文本库建索引（图文库由外部 CLI 管理） |
| `rerank` | `(kb, query, candidates, top_k=5)` | 仅文本候选精排；不发送图片 |
| `embed` | `(text="", image=None)` | 底层 embedding（文本 4096 维 / 图片 2048 维） |
| `ocr` | `(image_or_pdf, use_vlm=False)` | 调 VLM 识别图片/PDF，不是只读状态查询 |
| `status` | `()` | 主动健康检查，包含真实文本 embedding/rerank 推理，不是纯只读状态 |
| `list_models` | `()` | 对话端点当前加载的模型 + 显存 |
| `load_model` | `(model, gpu="auto", ttl=None, context_length=None, parallel=1)` | 装载对话模型 |
| `unload_model` | `(model="all")` | 卸载对话模型释放显存 |

> `list_models`/`load_model`/`unload_model` 只管对话模型；检索模型由 llama-swap 自己 TTL 管，
> 只查看时用 `GET http://127.0.0.1:9123/running`。不要用 `status` 替代只读查询，
> 也不要顺带发送 POST 卸载请求；显存调度须单独授权。

## 知识库检索规则

- **文本检索**默认走 `search(kb=..., query=...)`，不要默认全文 grep。
- **图文检索**：文本搜图传 `query`，以图搜图传 `image` 路径。
- 检索不到时先核对查询、真源与覆盖；重建涉及写入，须有相应授权，
  不以空结果自动触发重建。
- 图文库的索引由它自己的 CLI 管理（`retrieve build` 一类），本 skill 不重复建；
  接口可调用 ≠ 全库已验收。

## 模型清单与选型定论

| 用途 | 模型 | 位置 |
|---|---|---|
| 文本 embedding | `text-embedding-qwen3-embedding-8b`（8B，4096 维，MRL 可截 1024） | 检索端点 9123 |
| 文本 rerank | `text-reranker-8b`（8B） | 检索端点 9123 |
| 图文 embedding | `vl-embedding-2b`（2B，2048 维） | 检索端点 9123 |
| 图文 rerank | `vl-reranker-2b`（2B，自转 Q8_0） | 检索端点 9123 |
| 对话/视觉 | 视觉语言模型（VLM），支持工具调用 | 对话/视觉端点（默认 8080） |

**选型结论**：文本侧 = Qwen3-Embedding-8B + Qwen3-Reranker-8B；图文侧 = Qwen3-VL-Embedding-2B +
Qwen3-VL-Reranker-2B。按模态分专才，**不要**为"统一"换成大而全的单模型。

- **文本 rerank 用转换质量好的那份 GGUF**：缺 `cls.output` 张量的转换会返回垃圾分数（判据见下节）。
- **图文侧用 2B embed + 2B rerank**：llama.cpp 对 2B 有实测验证；更大的 rerank 属可选升级。
- **别用 VL 大模型统一文本+图文**：官方指引"纯文本用文本模型，有视觉才用 VL"。
- **图文 embedding 分辨率**：客户端降采样到 1152px（官方 max_pixels=1310720），
  单 tile 784px 会丢细节。
- **不要用 MRL 截维**：文本保持原生 4096。依赖客户端 `slice` 补丁的方案会在依赖升级后失效，
  导致维度反复回退并报 mismatch。

## 后端与已知坑

- **llama-swap `9123`**：检索统一入口。模型 spawn 到 10001+ 端口，TTL 默认 300s。
  改模型/加模型 = 改 `config.yaml` + **重启 llama-swap**（它只在启动时读配置）。
- **推理运行时**：Vulkan 版 llama-server 走 GPU；CPU 版慢，仅作回退。
  llama-server 必须显式 offload 全部层（`-ngl 99`）才用 GPU。
- **为什么选 Vulkan 而不是 CUDA**：选它是为了"能上 GPU"（当时手里只有纯 CPU 构建）。
  三条理由——自包含（走驱动自带 Vulkan，无需 CUDA runtime / PATH 折腾）、
  对新架构（如 Blackwell sm_120）的支持更确定性、体积显著更小（32MB vs 512MB）。
  **稳态下 Vulkan 反而更快，"换 CUDA 提速"不成立**——同 GGUF、批 32 × 4 轮取平均实测：

  | | 平均 | 每条 |
  |---|---|---|
  | Vulkan（llama-swap） | **309 ms** | 9.7 ms |
  | CUDA（另一路） | **561 ms** | 17.5 ms |

  单条查询两者都 ~16–18 ms（无差异）。这是"llama-swap 的 Vulkan 配置 vs 对话端的 CUDA 配置"
  对比（flag/ctx 不同），但方向在重复测量下稳定。
- **对话端点自带的 CUDA llama-server 不能独立启动**：直接运行报 `0xC0000135`
  （STATUS_DLL_NOT_FOUND）——`cublas64_*` / `cudart64_*` 在同级 `backends/vendor/` 里，
  靠宿主程序注入 DLL 搜索路径。它是对上游的 fork，**不是可直接拿来 A/B 的通用构建**。
- **GGUF 转换坑**：Qwen3-(VL-)Reranker 的 GGUF 必须含 `cls.output` + rank pooling。
  老转换（缺张量）→ 只能自转：本地 safetensors + 官方 `convert_hf_to_gguf.py`
  （cls 来自 lm_head 的 yes/no 行）。转换目录名要含模型名（检测靠目录名/README）。
- **模型文件身份判据（别按名字猜）**：图文 reranker 需要**一对**文件——
  文本骨干（arch = `qwen3vl`，含 `cls.output` + reranker chat template + `pooling_type`）
  与配套视觉塔 / mmproj（arch = `clip`，全 `v.blk.*`）。两者 `general.name` 相同（同一次转换产出），
  **配置只引文本骨干那一份**，另一份不是垃圾、别删。
  判据：**有 `cls.output` + `pooling_type` 键 = 好转换；只有 `output.weight` = 坏转换。**
- **图文 embedding 协议坑（llama.cpp）**：`multimodal_data` 要**裸 base64**
  （不要 `data:image/...` 前缀）；webp 等格式解不了 → 客户端 PIL 转 PNG；
  超大图降采样到 1152px 防 413。
- **MCP 开发坑**：stdio server 里 subprocess 必须 `stdin=DEVNULL`（否则继承 MCP stdin 卡死）；
  旧版 FastMCP 禁 `from __future__ import annotations`。

## 配置级陷阱与不变量

检索栈反复出事的是**同一族**，三种形态：**冷启动 vs 超时**、**无效配置行**、**静默降级**。
改 `ttl` 或改任何调用方 timeout 之后，必须重核下面两节。

### 不变量：冷加载实测值 < 所有调用方的最小 timeout

**每个 `ttl > 0` 的模型，其冷加载实测值必须小于调用它的所有客户端里的最小 timeout。**
违反时症状不是报错，而是**首次调用被误报"服务不可用"，或静默降级为低质量结果**。

实测（llama.cpp Vulkan，32GB 显卡）：

| 模型 | `ttl` | 冷加载实测 | 热态 |
|---|---|---|---|
| `text-embedding-qwen3-embedding-8b` | 300 | **4.29s**（2026-09-20 实测两次 4.285/4.29） | 0.018s（2026-09-20） |
| `text-reranker-8b` | 300 | **12.48s**（历史最坏 24.27s） | 1.03–1.13s |
| `vl-reranker-2b` | 300 | **16.27s** | — |
| `vl-embedding-2b` | 300 | **12.31s** | 0.02s |

**最小的调用方 timeout 是 agent 记忆检索的 15s，硬编码不可配**
（`DEFAULT_MEMORY_SEARCH_TIMEOUT_MS = 15e3`，本机定位方法见
`tools/governance/verify-local-models.ps1` 的注释）。比对冷加载时要拿它当上界，
而不是拿 skill/MCP 的 180s——
`tools/governance/verify-local-models.ps1` 目前只从 skill 层 `rag_client.DEFAULT_TIMEOUT`(180)
与 mcp 层 `image_retrieval`(300) 取最小值，**覆盖不到这 15s**，改 ttl 时须手工核。

**Muse Glimmer 30B 不在本表**：它由 8080 的 CUDA 常驻实例提供（无 TTL 卸载），
llama-swap 里的那份重复登记已于 2026-09-20 移除（显存装不下两份）。

**`groups.*.persistent: true` 只防 swap 驱逐，不防空闲 TTL 卸载。** 组 persistent 而成员
`ttl: 300` 时，5 分钟空闲后首次搜索仍要付 ~12.5s 冷加载。要抹平就把该模型也设 `ttl: 0`
（代价：常驻显存）。

### 容量不变量：30B 与检索模型不可同时驻留

实测（RTX 5090 D，32.6 GB）：30B 常驻 ~22 GB + 两个 8B 检索模型 ~10 GB = **超订**。
症状：显存 96%、rerank 请求 `TimeoutError`（`rag_client.DEFAULT_TIMEOUT=180s` × 5 次重试
≈ 15 分钟）→ 上层表现为"检索不可用"（⚠️ agent 记忆检索的 15s 硬上限必然先超时）。

**优先级（2026-09-20 定）：向量/检索模型 > 30B。**

| 角色 | 装卸方式 | 说明 |
|---|---|---|
| 检索模型（embed/rerank） | llama-swap TTL 自动 | 用完 5 分钟自动卸载，无需干预 |
| 30B（Muse Glimmer） | **手工启停，无 TTL** | 只在打标 / 批量视觉时按需起，用完停 |

冲突时**通知用户**调度启停：不要自行调度，也不要降级硬跑。

### 性能测量陷阱：测稳态，别测瞬态

推理后端有**巨大且不可预测的预热瞬态**，一次性测量必然得出错误结论。实测瞬态/稳态比：

| | 首个请求 | 稳态 | 倍数 |
|---|---|---|---|
| Vulkan 批 32 | 2,106 ms | **309 ms** | 6.8× |
| CUDA 批 32 | 612 ms | 561 ms | 1.1× |
| Vulkan 单条 | 298 ms | 16 ms | 19× |
| CUDA 单条 | **3,779 ms** | 17 ms | **222×** |

后果是实打实发生过的：同一组模型各测**一次**，得出"CUDA 快 3.3 倍"（先撞上 Vulkan 瞬态、
又撞上 CUDA 瞬态）；**重复测量后才看清是 Vulkan 快 1.8 倍**。

**规矩**：任何后端/配置对比 ——
1. 每边先跑 **2 轮丢弃预热**；
2. 至少 **4 轮取平均**；
3. 报告**全部原始值**，不只报平均（方差本身就是结论）；
4. 记下当时**显存占用**——显存吃紧会引入 JIT 装卸 churn，瞬态更离谱；
5. **变量只留一个**；比 bulk 吞吐（页/秒）而不只比单请求延迟。

**已排除的混淆项（不必重测）**：llama-swap 的代理开销 ≈ 0 —— 代理端口单条 165ms vs
直连 165ms 级，批 32 为 1997 vs 2038ms。

**做 A/B 时不要违反红线**：为了测 CUDA 而把 `text-embedding-*` load 进对话端点，
既违反红线，实测还会引发十几 GB 的显存 churn。**测完当天就卸掉。**

### 无效配置行（看着承重其实不承重）

- 模型下配 `env: LLAMA_MEDIA_MARKER=<__media__>` 是 **no-op**：该变量在 llama-server /
  mtmd / ggml 库里都不存在；`<__media__>` 是 `mtmd` 编译期默认 marker（与 `<|image>` 等并列）。
  当前值恰好一致所以无害，但它不提供任何保障。**图片走多模态的真实契约是
  "裸 base64 + 客户端转 PNG + 降采样 1152px"**，与此 env 无关。

### 静默降级

- 检索失败时用裸 `except Exception:` 吞掉 rerank 失败、返回 embedding 排序结果，只把
  `ranked_by` 标成 `"embedding"`——这是**静默降质**。保留兜底，但**必须写 stderr 日志**。
- **新增此类分支时，诊断必须写 stderr**：MCP stdio server 的 stdout 是 JSON-RPC 通道，
  `print()` 会污染协议。

### 已核对健康（勿重复排查）

- SDK 侧的 `dimensions` 参数会被 llama-server **静默忽略**（返回 200 + 全维向量，
  上限 PR 未合入）。要截维只能在客户端做 `slice` + 重归一，且**不要**这么做（见上「不要用 MRL 截维」）。
- `--embd-normalize 2` 确实生效：实测 embedding L2 范数 = 1.000000。
- 健康检查超时是**轮询探测**，不是单次定生死：12–24s 才就绪的模型也能通过。
- `nvidia-smi --query-compute-apps=used_memory` 在 WDDM 下恒为 `[N/A]`，**查不到逐进程显存**，
  别用它做归因（要看 `lms ps` + `GET /running`）。

## 数据治理框架：真相源 → 检索投影

**主从关系**：原始文件/笔记库是唯一权威；向量库/文本索引只是它的
**可搜索投影** —— 单向 build、永不反向写，可再生。
检索闭环：提问 → 投影（分区路由 + 混合检索）→ 返回 `source_path` → 回真相源 Read 原文。

**各域单向同步链路（无通用 watcher，增删源文件后需手动或 cron 触发 build）**：

| 真相源 | 检索投影 | 构建命令 |
|---|---|---|
| 文档/笔记目录 | `~/.local-rag/lancedb`（LanceDB，表 kb_<name>） | `cli.py --kb <name> index` |
| 图文语料 | 图文库自己的 LanceDB | 由外部 CLI 的 `retrieve build` 一类命令 |
| 结构化事实 | 数据槽位（sqlite / 外部数据源），**不进 embedding** | 入库脚本 |

**中文关键词硬约束**：纯向量库**没有中文分词**，中文场景必须走
BM25（ngram）/FTS trigram，勿用纯向量替换混合检索。

## 扩展方式

新增一个知识库 = 在 `registry.yaml` 的 `knowledge_bases` 加一段：

- 文本库：`type: text` + `root` + `patterns`（子目录用 `path_filter` 复用父索引）。
- 图文库：`type: multimodal` + `endpoint` + `dimensions` + 模型 id + 外部 CLI 路径。
- 本机私有路径写进 `registry.local.yaml`（不进 git），合并规则见 README。

## 可插拔垂类库（Vertical Package）规范

大型垂类做成**自包含、可挂载/卸载、独立投影**的包。完整 schema（manifest 字段 / 后端分档 /
agent 调用协议 / 生命周期 / 示例）见同目录 `kb-manifest-schema.md`。要点：

1. **独立性在投影 + 生命周期层**：同源文本共享检索引擎 + `path_filter` 分区视图，
   **禁止同语料重复嵌入**；图文/结构化/超大才建专属后端（own LanceDB / DB / 工具槽位）。
2. **挂载/卸载 = 注册表增删一条**，真源与已建投影保留；agent 永不反向写。
3. **agent 判定调用**：先分事实 vs 语义——精确数字走 data 槽位，观点/案例走 `search(kb)`；
   读 `list_kbs` 的能力描述挑包，拿不准收窄到 ≤2 候选。
4. **分档**：T0 事实 / T1 同根文本视图 / T2 独立 root 文本 / T3 图文专属包。
5. **现状/缺口**：文本引擎已内置孤儿 prune（`index()` 默认清理已删源文件的 chunk，
   仅全量扫描时执行）；`max_files` 默认 10000，语料超限时该 root 的 prune 自动停用。

## 新增 / 改造一个域库（runbook）

1. **判档**：结构化事实 → T0 数据槽（DB/数据源适配器，不进语义）；同 root 子域 → T1
   `path_filter` 视图；独立 root 纯文本 → T2 文本；图文/超大/异构 → T3 multimodal 自打包。
2. **单一真相源**：确认语料无第二真相源、无既有投影重复；有 → 先归位/复用，绝不重复嵌入。
3. **manifest**：照 `kb-manifest-schema.md` 写；`capability` 填
   `good_for/not_for/example_queries/freshness`（agent 路由依据）。
4. **挂载**：semantic → `registry.yaml`（或私有 `registry.local.yaml`）加段；
   data → 数据工具注册表；T3 → 自打包（own LanceDB）。
5. **build + 验证**：`index(kb)` / 外部 CLI build / 灌数脚本 → `list_kbs` + `search(kb, query)` 命中核验。
6. **路由生效**：capability 描述同步进目录（`list_kbs` / MCP server instructions）。
7. **生命周期记档**：记录该域 增/改/删 的 build/sync/prune 命令与已知缺口。
