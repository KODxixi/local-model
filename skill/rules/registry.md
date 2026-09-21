# 配置说明

> 真相源：`registry.yaml`（公开模板）+ `registry.local.yaml`（本机私有）

## 配置分层

| 文件 | 用途 | 是否进 git |
|------|------|-----------|
| `registry.yaml` | 公开模板，只有注释示例 | ✅ 是 |
| `registry.local.yaml` | 本机真实配置（路径、库名） | ❌ 否（gitignore） |

`load_registry()` 自动合并本地覆盖。

## 模型清单

| 模型 | 用途 | 维度 | 端点 |
|------|------|------|------|
| text-embedding-qwen3-embedding-8b | 文字向量 | 4096 | 检索端点（llama-swap 9123） |
| vl-embedding-2b | 图向量 | 2048 | 检索端点（llama-swap 9123） |
| vl-reranker-2b | **文本 + 图文精排（共用）** | - | 检索端点（llama-swap 9123） |
| Muse Glimmer 30B + DFlash + Vision | 对话/视觉主模型（`ttl: 900` 空闲自卸） | - | llama-swap 9123 (CUDA + DFlash)，`muse-glimmer-30b` |

> `text-reranker-8b` 已于 2026-09-21 从配置移除：8B 版 @`-c 4096` 实测占 6.45 GB，
> 与常驻 30B 相加超订。改用 `vl-reranker-2b` 兼任，
> 排序质量 A/B 无差异（Top-1 6/8 打平，MRR 0.8375 vs 0.8000），详见
> `C:\AI\tools\llama-swap\rerank-ab-20260921.md`。GGUF 仍在磁盘，可手工加回（须先停 30B）。

## 磁盘上的储备模型（**在用但未登记 —— 别当尸体删**）

| 模型 | 大小 | 状态 |
|---|---|---|
| `VesNFF/Qwen3-VL-Embedding-8B-GGUF`（Q6_K + mmproj） | 6.87 GB | **储备**：未登记任何端点。是"图文检索升级 ViDoRe +5.9"的采购（需 safetensors 8B 自转 GGUF，本地只有 2B safetensors）。2026-09-21 审计时确认保留，理由：删不可逆、占 0 显存、重下成本高 |
| `Qwen/Voodisss-Qwen3-Reranker-8B`（Q4_K_M） | 4.36 GB | **备用**：2026-09-21 起不在 config.yaml 登记（与常驻 30B 算术冲突）。要启用必须先手工卸下 30B（`curl -X POST 127.0.0.1:9123/api/models/unload/muse-glimmer-30b`） |

> 已删（2026-09-21，不可恢复）：`lmstudio-community/Qwen3-VL-8B-Instruct-GGUF`（5.76 GB）、
> `mradermacher/Qwen3-Reranker-8B-GGUF`（4.47 GB）、`mradermacher/Qwen3-VL-Reranker-2B-GGUF`（2.47 GB）、
> `Qwen3-VL-Reranker-2B-archlib-q8_0.gguf`（0.41 GB）。合计 12.4 GB。

## Muse Glimmer 30B 参数

- 上下文：**16,384**（单槽即全量）。⚠️ 服务端参数的**唯一真相源**是
  `C:\AI\tools\llama-swap\config.yaml` 的 `muse-glimmer-30b` 条目；本节是镜像，改参数先改那里。
  `n_ctx_train` 是 131,072；128K 全开 + 检索栈共存会超订。2026-09-21 甲-1 由 32768 降到 16384 ——
  代价是打标的 `max_tokens=24576` 会被 llama-server **静默钳位**到 ~13.5k（实测不报错；
  实测产出仅 ~750 token，故不受影响）
- 生成速度：125-220 tok/s（DFlash 投机解码）
- GPU：实测 **20.0 GB** / 32.6 GB（含 mmproj + dflash + KV，单槽 `--parallel 1`）
- 能力：文本对话 + 视觉理解（Vision）+ 工具调用（Tool use）
- 生命周期：由 llama-swap 托管，`ttl: 900`（空闲 15 分钟自卸，冷加载实测 ~54s）。
  甲-1 前的独立实例路径（计划任务 `MuseGlimmer` + `start-muse-8080.ps1`）已退役，
  任务置 **Disabled** —— 启用会起第二个 30B → OOM
- 改参数须同步 skill 文档。真相源分配表见 `AGENTS.md`。
- 推荐打标参数：**max_tokens=24576（视觉类）/ 12288（分析类）**、temperature=0.1、
  `reasoning_effort=low`（2026-09-21 调高：单槽 32768 后 提示词 ~2,829 + 24576 = 27,405，余 5,363）。
  调高目的是**防止 reasoning 吃光配额导致 `content` 为空**（实际只产出 ~750 token，个别图会暴涨）。
  ⚠️ **max_tokens 与客户端超时必须同抬**：超时 90s 在 ~130 tok/s 下只能生成 ~11,700 token，
  更高的额度走不到。

## 视觉打标架构

- 视觉主模型直接看图打标（30B 的视觉理解能力更强）
- 单模型完成：看图 → 理解 → 输出结构化标签
- 推理模型只取 `content` 字段，忽略 `reasoning_content`
