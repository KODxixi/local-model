# AGENTS.md — local-model

> agent 第一入口：只存指针。规则全文在独立 .md。
> **路径约定**：以下命令假设你在 skill 根目录（AGENTS.md 所在目录）运行。
> 如果不在，先 cd 到 skill 根目录。

## 🚀 快速上手

**一句话**：把任意杂乱文件变成 agent 可命中的语义知识库。

```bash
# 0. 先看看有哪些知识库（doctor 输出里会列出所有 kb 和状态）
python scripts/cli.py doctor

# 1. 诊断系统状态（模型在线？索引存在？知识库有覆盖？）
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
- **检索结果为什么命中？** → 加 `--explain` 参数
- **文件怎么解析？** → 跑 `python scripts/cli.py ingest <文件> --json`
- **模型在线吗？** → 跑 `python scripts/cli.py doctor`，看 9123/8080 状态

## ❌ 负面指针（不要去哪找）

- ❌ 不要去 `mcp/` 目录找 — 已删除，MCP 层已砍掉
- ❌ 不要去 `knowledge_graph.py` 找 — 已删除，kg 命令已砍掉
- ❌ 不要去 `visualize.py` 找 — 已删除，kg 可视化已砍掉
- ❌ 不要找 `test_retriever_innovations*.py` — 已删除，MMR/parent-child/多查询扩展已砍掉

## 模型配置速查

| 端口 | 用途 | 模型 | 红线 |
|---|---|---|---|
| 9123 | embed/rerank | text-embedding-qwen3-embedding-8b (4096维) | 禁止跑对话/VLM |
| 8080 | chat/VLM | Muse Glimmer 30B + DFlash + Vision | 禁止跑 embed/rerank |

**为什么**：embed/rerank 是向量计算，需要 GPU 全层 offload；chat/VLM 是生成式任务，两者显存需求不同，不能混用。

## 红线

- embed/rerank 只走 9123，对话/视觉只走 8080
- 索引是可再生投影，永不反向写
- 真相源：rules/redlines.md

## 常见错误速查

| 错误码 | 含义 | 怎么办 |
|---|---|---|
| `E_DEPENDENCY_MISSING` | 依赖未安装 | 跑 `setup.ps1` 安装依赖 |
| `E_CONNECTION_REFUSED` | 模型服务没启动 | 检查 9123/8080 端口，看 rules/troubleshooting.md |
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
- doctor 输出含义：rules/doctor-output.md（待建）

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
