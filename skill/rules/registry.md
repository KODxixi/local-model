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
| text-reranker-8b | 文本精排 | - | 检索端点（llama-swap 9123） |
| vl-reranker-2b | 图文精排 | - | 检索端点（llama-swap 9123） |
| Muse Glimmer 30B + DFlash + Vision | 视觉/打标主模型（按需起） | - | 8080 (CUDA + DFlash) |

## Muse Glimmer 30B 参数

- 上下文：131,072 tokens（128K 原生；批量打标场景用 32768）
- 生成速度：125-220 tok/s（DFlash 投机解码）
- GPU：22-30 GB / 32.6 GB
- 能力：文本对话 + 视觉理解（Vision）+ 工具调用（Tool use）
- 启动命令：见 `SKILL.md` 的 "Muse Glimmer 30B + DFlash" 部分
- 推荐打标参数：max_tokens=16384, temperature=0.1, n_max=10

## 视觉打标架构

- 视觉主模型直接看图打标（30B 的视觉理解能力更强）
- 单模型完成：看图 → 理解 → 输出结构化标签
- 推理模型只取 `content` 字段，忽略 `reasoning_content`
