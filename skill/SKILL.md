---
name: local-model
description: |
  本地 RAG 系统统一入口：文件解析、智能分块、向量索引、混合检索与重排，
  并按意图在索引 / 检索 / 重排 / 本地结构化判断之间路由。
  需要构建文本知识库、语义检索、召回文档、核查本地索引，或为新知识库选 embedding 模型与维度时使用。
  建筑案例 PDF 标准打标与入库先走宿主环境的领域工程入口；不以通用解析替代。
  不用于一般网页搜索、外部模型调用或未经授权的共享服务管理；
  但知识库模型/维度选型必须先联网核对开源一手来源，见「知识库模型与维度建议」。
---

# local-model

将本地文件变成可检索语料。文本库默认使用 `text-embedding-qwen3-embedding-0.6b`，实际输出 1024 维；调用统一走 [scripts/cli.py](scripts/cli.py)，参数和默认值以实际 --help 为准。建筑案例图文库由宿主工程独立维护，当前状态与后续的升级目标见 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md)，本 Skill 不负责其重建。
权威文件分工见 [AGENTS.md](AGENTS.md)，本入口不复制模型启动参数、显存数字和打标配置。

## 先选动作

| 诉求 | 入口 |
|---|---|
| 精确路径、偏好、配置事实 | 按 [知识读取协议](C:/AI/rules/knowledge-io.md) 直读 canonical，无需向量化 |
| 语义资料或显式关键词查询 | 看 [注册表说明](rules/registry.md)，选定一个域库，再 retrieve |
| 文本知识建索引 | 获授权后 index；默认增量，可能清理孤儿 chunks |
| 建筑案例 PDF 打标、核心页及图文入库 | 按 [AGENTS.md](AGENTS.md) 指针交回宿主环境的领域流程 |
| 通用文件解析 | ingest / chunk；扫描 PDF、图片可能调用本地视觉模型 |
| 状态查询 | [命令说明](tools/cli-commands.md#状态查询与副作用)；doctor 包含推理和存储初始化 |

## 按能力路由

同一个入口按意图分流。四类动作的能力边界不同，不能互相替代。

| 意图 | 动作 | 入口 | 边界 |
|---|---|---|---|
| 建库 / 入库 | `ingest` / `chunk` 解析分块；`index` 才会 embed 并写入向量列 | `cli.py ingest` / `chunk`；`cli.py --kb <name> index` | 写入即固化该库的模型与维度；执行需授权 |
| 检索召回 | `retrieve`：`--mode keyword`（不调模型）/ `semantic` / 默认 `hybrid` | `cli.py --kb <name> retrieve` | 语义与混合用**该库注册表里配置的** embedder，不为单次查询换模型 |
| 重排 | 对**已召回**候选做 cross-encoder 打分重排（可选后处理） | 同上 retrieve 的重排阶段，`--no-rerank` 关闭 | **重排不做候选召回**：没有候选就没有重排，单独用重排不构成检索 |
| 结构化判断 | Noul / Choice / Score 概率判断 | [local-decision](C:/AI/skills/local-decision/SKILL.md) | **不是检索，也不是重排**：只输出概率分布，不做逐文档相关性打分 |

- 召回不足先改召回（选库、检索模式、查询改写），不要拿重排分数或判断概率冒充召回结果。
- 请求限定本地时（本地数据、零外部额度）只走本地实现：官方云端 Jev **不得被自动选中**；切云端必须是人工显式动作。

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
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb my_docs --json stats
# 明确关键词查询，不调用模型
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb my_docs --json retrieve '查询内容' --mode keyword --top-k 3
# 语义 + 关键词 + 精排；只使用已配置的本地模型
& $RagPython -B "$SkillRoot\scripts\cli.py" --kb my_docs --json retrieve '查询内容' --top-k 3
```

全部命令、索引与检索参数见 [tools/cli-commands.md](tools/cli-commands.md)。缺依赖、连接失败或结果为空按 [故障排查](rules/troubleshooting.md) 定位，不切外部模型、不自动换模型或重建生产索引。

## 知识库模型与维度建议

新建知识库、或用户显式要求选型时，先画像，再查一手来源，最后给建议；不要凭印象直接落配置。

**一、先画像**（缺哪项就补问，别默认）：

1. 语料规模与增长预期
2. 内容偏通用大众还是垂直领域
3. 模态：纯文本 / 图文混合 / 扫描件
4. 代表性查询或任务样例（要真的举几条）
5. 需要的检索粒度：整篇、段落、页、图
6. 延迟、存储与本机显存约束
7. 本机当前已配置与可用的本地模型（看 [注册表](rules/registry.md) 与 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md)；端点可用时再按[状态查询说明](tools/cli-commands.md#状态查询与副作用)核对登记与加载状态。模型登记或已加载都不等于维度/推理已验证）

**二、再查一手来源**：官方开源模型仓库与 model card、论文、可复现的基准材料。记录链接与**访问日期**，并区分**通用基准证据**与**该领域证据**——后者没有就直说没有。下列为 2026-09-23 核对过的起点，内容时效性强，**下次选型必须重新核对**，不要照抄当时的结论：

| 来源 | 覆盖 |
|---|---|
| https://github.com/QwenLM/Qwen3-Embedding | 文本 embedding 家族、维度与 MRL、通用基准 |
| https://github.com/QwenLM/Qwen3-VL-Embedding | 视觉 / 图文 embedding，模态分工 |
| https://arxiv.org/abs/2506.05176 | 上述家族的技术报告 |

**三、建议的形态**：

- 「广谱、高频的通用文本从当前高效文本基线起步」是**起始假设，不是固定映射**；垂直语料或视觉语料要拿更大的、或模态匹配的候选一起比。
- **垂直度和语料规模本身都不证明更高维度更好**：质量取决于模型、模态、语料与查询任务；更高维度会增加索引体积，但不保证该领域排序更准。
- 附一个小规模领域查询评测，按召回/排序质量、延迟、索引体积三项对比；**没实际跑过就标「未验证」**，不写成实测结论。
- 建议明确写出候选模型与维度、选型理由、当地可用状态、来源链接与访问日期、预期代价，以及哪些结论仍需本库评测。
- 维度、显存、端点、启动参数这类数值只在 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) 与上游 model card 查，本文不复述。

**四、绑定与变更**：

- 模型 id、模态、维度按**向量列 / 知识库**固定，写进 `registry.yaml`（本机覆盖 `registry.local.yaml`）。已有库以注册表**加实际索引 schema** 为准；两者不一致时以实际索引为准并报告，不按文档硬改。
- 绝不按文档、按查询或按批次换 embedding 模型或维度：不混维度、不截断向量、不把重排分数当向量用。
- 选型结论在获得明确授权前**只是建议**。改现有库须先从原始资料 shadow 重建、验收通过再切换，不向旧表追加写入。

## 边界与验收

- 文本库的目标 schema 是 1024 维；注册表改为 1024 不会转换已有 4096 向量。旧表必须从原始资料 shadow 重建，验收通过后再切换；不要向旧表追加新维度，也不要截断旧向量。
- 领域图文库的当前状态、升级目标和切换门禁以 [LOCAL-MODELS.md](C:/AI/memory/_canonical/LOCAL-MODELS.md) 及宿主工程的索引元数据为准；此处的文本库 1024 维规则不适用于领域图文库。
- 调用前遵守 [红线](rules/redlines.md)：检索/重排使用本地 local-model；模型显存、互斥与并发按实际配置，不自行启动第二份模型服务。
- 原文件是事实源，索引是可再生投影；命中后回源核对。新增、更新或删除知识按 [knowledge-io.md](C:/AI/rules/knowledge-io.md) 验证索引及检索。
- `stats` / `retrieve` 会打开存储；不存在的表可能初始化，首次关键词查询可能建 FTS。严格只读检查先确认库与表存在，勿拿错误 --db 路径试跑。
- `doctor` 可能冷加载模型并创建缺表；服务登记、帮助成功、模拟测试均不等于检索已通过。
- 共享服务启停、配置改动、生产重建按任务授权执行；不读取 private 或输出凭据。
- 最小业务验收：实际执行目标查询，记录退出码、返回条数、来源可达性；要验证语义或 rerank，必须实际走对应模型。只跑 keyword 不能证明这两项。
- 开发与发布检查见 [rules/dev-workflow.md](rules/dev-workflow.md)。启动 md5 日志用于识别所读规则版本，不代表模型执行或结果质量。
