# AGENTS.md — local-model

> Agent 第一入口；以下相对路径均以本 Skill 目录为基准，命令工作目录与解释器见 SKILL.md。

## 按任务找入口

| 我想做什么 | 唯一入口 |
|---|---|
| 第一次调用、安装依赖、确认权限边界 | [SKILL.md](SKILL.md) |
| 选知识库、看本机配置如何覆盖模板 | [注册表说明](rules/registry.md)；实际加载逻辑见 [rag_indexer.py](scripts/rag_indexer.py) 的 load_registry |
| 查已有文本库条数、运行检索、查看参数 | [CLI 命令](tools/cli-commands.md)；参数最终以 [cli.py](scripts/cli.py) 的 --help 为准 |
| 查 Archlib 图文库或进行标准 PDF 打标入库 | [Archlib 工程入口](C:/GarchOS/archlib/AGENTS.md)；不得用通用文本库 stats 的 0 断言图文库为空 |
| 查服务登记与已加载状态 | [CLI 的状态查询](tools/cli-commands.md#状态查询与副作用) |
| 改模型配置、并发或显存参数 | [显存和端点红线](rules/redlines.md)，再读下方权威分工 |
| 查询失败、索引锁、缺依赖 | [故障排查](rules/troubleshooting.md) |
| 解释 doctor 结果 | [doctor 输出](rules/doctor-output.md) |
| 修改代码、跑测试 | [开发流程](rules/dev-workflow.md) |
| 查设计理由与历史实验 | [决策记录](rules/decisions.md)、[完整工作流](references/full-workflow.md)；历史数值不覆盖当前配置 |

## 真相源分配表

| 内容 | 真相源 |
|---|---|
| 跨项目配置背景、显存预算与消费方 | [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) |
| 服务模型、端点、启动参数与生命周期 | [llama-swap config.yaml](C:/AI/tools/llama-swap/config.yaml) |
| 知识库模板 / 本机覆盖 | [registry.yaml](registry.yaml)、registry.local.yaml（可选、未入 Git）；格式见 [注册表说明](rules/registry.md) |
| 打标参数 | [生产 retag_empty.py](D:/Archlib_V2/runtime/retag_empty.py) |
| 建筑打标 prompt | [tagging-prompt.md](C:/GarchOS/archlib/system/tagging-prompt.md) |
| 通用 PDF VLM / 查询改写 / 文档摘要 prompt | [PDF](system/pdf-vlm-prompt.md)、[改写](system/query-rewrite-prompt.md)、[摘要](system/document-summary-prompt.md) |

## 不要走的旧入口

- 已退役的 local-models MCP、qwen-embedding 不再承担调用；使用上面的 CLI。
- 不使用已删除的 knowledge_graph.py、visualize.py、muse.py，也不把旧 kg / --expand / --mmr 示例当现役 CLI。
- 不手工启动旧 MuseGlimmer 独立服务；服务管理与授权见 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) 和 [红线](rules/redlines.md)。
