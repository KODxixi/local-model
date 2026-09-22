# local-model

本地文件检索工具：解析文件、分块、建立 LanceDB 索引，并用本机模型完成语义召回、中文关键词检索和精排。`scripts/cli.py` 是命令入口；模型由 llama-swap 管理。

公开仓库中工具位于 `skill/`。下方 `<SKILL_ROOT>` 指该目录；母库安装时指实际 Skill 目录。完整参数以 `scripts/cli.py --help` 为准。

## 能处理什么

- 文本：Markdown、纯文本、代码及常见办公文件；按知识库分别建表。
- PDF：优先提取文字，扫描页可调用本地视觉模型；页面图向量单独建表。
- 检索：语义向量、中文 ngram BM25 关键词、RRF 混合召回和 cross-encoder 精排。
- 诊断：`stats` 看已有索引；`freshness` 比对源文件；`doctor` 会发起真实模型请求，也可能初始化存储。

文本库的新默认模型是 `text-embedding-qwen3-embedding-0.6b`，输出 **1024 维**。视觉模型 `vl-embedding-2b` 输出 **2048 维**，页面图及独立案例图文库保留其自身维度。精排模型只返回相关性分数，没有 embedding 维度。现有 4096 维表必须从原始资料在新表重建并验收，不能用 `--force` 改变旧表 schema，也不能截断旧向量。

## 安装与使用

在 Windows PowerShell 中，将 `<SKILL_ROOT>` 换成实际路径。首次安装运行 `setup.ps1`；它会安装依赖并运行包含真实模型调用的诊断。

```powershell
$SkillRoot = '<SKILL_ROOT>'
& "$SkillRoot\setup.ps1"
$Python = "$SkillRoot\.venv\Scripts\python.exe"
& $Python -B "$SkillRoot\scripts\cli.py" --help
```

在 `registry.local.yaml` 中填写自己的知识库路径；该文件不提交到 Git。`registry.yaml` 是可公开的示例及默认设置。以下命令中的 `my_docs` 换成已注册库名，`--kb` 等全局参数须放在子命令之前。

```powershell
& $Python -B "$SkillRoot\scripts\cli.py" --kb my_docs stats
& $Python -B "$SkillRoot\scripts\cli.py" --kb my_docs freshness
& $Python -B "$SkillRoot\scripts\cli.py" --kb my_docs index
& $Python -B "$SkillRoot\scripts\cli.py" --kb my_docs retrieve '查询内容' --top-k 5
& $Python -B "$SkillRoot\scripts\cli.py" --kb my_docs retrieve '精确短语' --mode keyword --top-k 5
```

`index` 默认只处理新增或变更的源文件，并清理已删除文件对应的片段。`--force` 会重新处理源文件，但不会转换现有表的维度。`retrieve` 默认做混合召回与精排；`--no-rerank` 可单独检查召回。`--json`、`--db` 和 `--registry` 是全局参数。

## 存储与迁移

默认数据库位于 `~/.local-rag/lancedb`，每个文本知识库使用一张 `kb_{name}` 表；PDF 页面图存入独立的 `kb_{name}_images` 表。源文件是事实源，向量库是可重建的投影。维度或模型变化时：先备份旧表与配置，再用新数据库从原始文件建立影子表，核对实际 schema、模型、文件覆盖和真实检索，最后切换读取入口；旧表留作回滚。

索引使用进程锁，不能并发建库。运行中的其他索引任务完成后再启动下一库。模型连接失败时会明确报错，不会调用外部 embedding 服务。检索命中后仍应回源文件核对内容。

## 开发与发布

`scripts/` 放实现，`tests/` 放回归检查，`system/` 放提示词，`rules/` 放配置说明。公开仓库是母本 Skill 的受管导出，修改请回到母本，再按其开发流程同步。仓库根 `AGENTS.md` 和 `skill/AGENTS.md` 为 agent 提供规则入口；README 只说明产品和使用方法。

```powershell
& $Python -m pytest "$SkillRoot\tests" -m 'not integration'
& $Python -m ruff check "$SkillRoot\scripts" "$SkillRoot\tests"
```

许可证：[MIT](LICENSE)。
