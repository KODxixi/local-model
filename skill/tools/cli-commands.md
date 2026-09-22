# CLI 命令与状态查询

命令真相源：[scripts/cli.py](../scripts/cli.py)。解释器、安装与完整调用方式见 [SKILL.md](../SKILL.md)。
以下以 `cli.py` 简写已选解释器加脚本绝对路径；不得把简写直接当系统命令。

## 参数与选库

`--kb / --db / --registry / --json` 均放在子命令之前。只有 index、freshness、stats、retrieve、search-image 要求 --kb；其他命令不需要。
注册表结构及本机覆盖见 [rules/registry.md](../rules/registry.md)，具体选库以 load_registry 合并结果为准。

| 任务 | 命令 |
|---|---|
| 查看实际可用命令 | `cli.py --help` / `cli.py retrieve --help` |
| 已有文本库统计 | `cli.py --kb <name> --json stats` |
| 新鲜度 | `cli.py --kb <name> --json freshness`（退出码 2 表示过期） |
| 混合召回与重排 | `cli.py --kb <name> --json retrieve "查询" --top-k 3` |
| 仅关键词，不调模型 | `cli.py --kb <name> --json retrieve "查询" --mode keyword --top-k 3` |
| 仅向量召回，不重排 | `cli.py --kb <name> --json retrieve "查询" --mode semantic --no-rerank` |
| 解释排序或限定路径 | retrieve 的 `--explain` / `--path-filter`；详情看子命令帮助 |
| 导出查询过程 | retrieve 的 `--trace <输出.html>`（会写文件） |
| 图文库以图搜图 | `cli.py --kb <图文库> search-image <图片>`；调用前确认域库协议 |
| 增量索引 | `cli.py --kb <name> index` |
| 全量重建 | `cli.py --kb <name> index --force`（需相应授权） |
| 增量索引保留孤儿 | `cli.py --kb <name> index --no-prune` |
| 通用文件解析 / 分块 | `cli.py ingest <文件>` / `cli.py chunk <文件>` |
| 查询改写 / 摘要 | `cli.py rewrite "查询"` / `cli.py summary <文件>`（调用本地模型） |
| 向量 / 重排调试 | `cli.py embed "文本"` / `cli.py rerank "查询" --docs 文档1 文档2` |
| 完整诊断 | `cli.py --json doctor` |

`retrieve --kb all` 不是合法参数顺序；合法的 `cli.py --kb all retrieve ...` 会扩大查询范围，默认跳过图文库，日常查询先选定域库。
领域图文库由宿主工程管理；不用文本库 stats 的 0 推断它为空，也不用通用 index 替代建筑 PDF 打标与图文索引流程。

## 状态查询与副作用

- 只验证入口：解释器 `--version` 和 `cli.py --help`，不调用模型。
- 查登记 / 已加载模型：对实际本机端点 GET `/v1/models` / `/running`；端点来自 [服务配置](C:/AI/tools/llama-swap/config.yaml)。前者包含未加载项，两者都不是推理证明。
- `stats` / `retrieve` 初始化存储对象，错误路径或缺表可能产生空库；只读查询先确认目标库和表存在。关键词路径可能首次建立 FTS。
- `doctor` 会做真实 embedding 检查并初始化知识库存储；可能加载模型或创建缺表，不是只读探活。
- 建索引、迁移、优化、清理与模型服务启停均按任务授权，命令存在不等于已获执行授权。

## 旧入口

❌ local-models MCP、qwen-embedding、muse.py、kg 命令已退役；不再据旧示例调用。
❌ `--expand / --mmr / --route / --parent-child` 不在当前 retrieve CLI；Python 内部能力不等于公开参数。
`migrate / optimize` 属于 [vector_store.py](../scripts/vector_store.py) 的独立命令，非 cli.py 子命令；按该脚本 --help 和任务授权使用。
模型管理规则只读 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) 与 [红线](../rules/redlines.md)，本文件不复制启动参数和卸载命令。
