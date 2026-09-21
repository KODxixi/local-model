---
name: local-model
description: |
  本地 RAG 系统统一入口：文件解析、智能分块、向量索引、混合检索与重排。
  需要构建文本知识库、语义检索、召回文档或核查本地索引时使用。
  建筑案例 PDF 标准打标与入库先走 archcase / Archlib 工程入口；不以通用解析替代。
  不用于网页搜索、外部模型调用或未经授权的共享服务管理。
---

# local-model

将本地文件变成可检索语料。调用统一走 [scripts/cli.py](scripts/cli.py)；参数和默认值以实际 --help 为准。
权威文件分工见 [AGENTS.md](AGENTS.md)，本入口不复制模型启动参数、显存数字和打标配置。

## 先选动作

| 诉求 | 入口 |
|---|---|
| 精确路径、偏好、配置事实 | 按 [知识读取协议](C:/AI/rules/knowledge-io.md) 直读 canonical，无需向量化 |
| 语义资料或显式关键词查询 | 看 [注册表说明](rules/registry.md)，选定一个域库，再 retrieve |
| 文本知识建索引 | 获授权后 index；默认增量，可能清理孤儿 chunks |
| 建筑案例 PDF 打标、核心页及图文入库 | [archcase](../archcase/SKILL.md)，按其工程指针进入 Archlib 标准流程 |
| 通用文件解析 | ingest / chunk；扫描 PDF、图片可能调用本地视觉模型 |
| 状态查询 | [命令说明](tools/cli-commands.md#状态查询与副作用)；doctor 包含推理和存储初始化 |

## 运行入口

`<SKILL_ROOT>` 指本 SKILL.md 所在目录。PowerShell 使用绝对解释器和脚本路径，从任何工作目录调用：

```powershell
$SkillRoot = 'C:\AI\skills\local-model' # 其他机器改为实际 Skill 目录
$RagPython = Join-Path $SkillRoot '.venv\Scripts\python.exe'
& $RagPython --version
& $RagPython -B (Join-Path $SkillRoot 'scripts\cli.py') --help
```

解释器启动失败时，先查 `.venv/pyvenv.cfg` 的基座是否存在及能否执行。不要仅因目录存在就宣称可用，也不要直接改 ACL 或重建环境。可选择已验证的本机 Python，并先核对 [requirements.txt](requirements.txt) 所需依赖；本机系统 Python 候选为 `C:\Program Files\Python313\python.exe`。瞬时失败写验收记录，不写成永久故障事实。

首次安装使用 [setup.ps1](setup.ps1)：它会创建环境、安装依赖并运行 doctor，含真实模型调用与存储初始化；只查入口时不要运行安装脚本。

## 最短调用

以下沿用上方变量；`--kb / --db / --registry / --json` 写在子命令前。查询只选对应域，不默认 `--kb all`。

```powershell
# 统计已有文本库；库名按本机注册表替换
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb openclaw --json stats
# 明确关键词查询，不调用模型
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb openclaw --json retrieve '查询内容' --mode keyword --top-k 3
# 语义 + 关键词 + 精排；只使用已配置的本地模型
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb openclaw --json retrieve '查询内容' --top-k 3
```

全部命令、索引与检索参数见 [tools/cli-commands.md](tools/cli-commands.md)。缺依赖、连接失败或结果为空按 [故障排查](rules/troubleshooting.md) 定位，不切外部模型、不自动换模型或重建生产索引。

## 边界与验收

- 调用前遵守 [红线](rules/redlines.md)：检索/重排使用本地 local-model；模型显存、互斥与并发按实际配置，不自行启动第二份模型服务。
- 原文件是事实源，索引是可再生投影；命中后回源核对。新增、更新或删除知识按 [knowledge-io.md](C:/AI/rules/knowledge-io.md) 验证索引及检索。
- `stats` / `retrieve` 会打开存储；不存在的表可能初始化，首次关键词查询可能建 FTS。严格只读检查先确认库与表存在，勿拿错误 --db 路径试跑。
- `doctor` 可能冷加载模型并创建缺表；服务登记、帮助成功、模拟测试均不等于检索已通过。
- 共享服务启停、配置改动、生产重建按任务授权执行；不读取 private 或输出凭据。
- 最小业务验收：实际执行目标查询，记录退出码、返回条数、来源可达性；要验证语义或 rerank，必须实际走对应模型。只跑 keyword 不能证明这两项。
- 开发与发布检查见 [rules/dev-workflow.md](rules/dev-workflow.md)。启动 md5 日志用于识别所读规则版本，不代表模型执行或结果质量。
