# AGENTS.md — local-model 第一入口

> ⚠️ **本仓是导出快照，不是真相源。** 内容由作者母库 `C:\AI\skills\local-model\` 手动导出，
> 本仓不做独立演进。下表里凡以 `C:\` 开头的绝对路径都指向作者本机母库，**在你这里不可达** ——
> 站内等价物见「去哪查」列。
>
> 只看这一份。其余文档按下方指针按需读，**不要预读全部**。

## 任务路由（外部 agent 查这里）

| 我想知道… | 去哪查 | ❌ 不要去 |
|---|---|---|
| agent 操作细则 / 真相源分配表 | `skill/AGENTS.md` | 不要预读 `skill/references/` 全篇 |
| 模型清单 / 端点 / 向量维度 | `skill/rules/registry.md` | ❌ 不要凭记忆答端点与维度 |
| 红线（破了会停服 / 打停别人的服务） | `skill/rules/redlines.md` | — |
| 排障（连不上 / 结果不对） | `skill/rules/troubleshooting.md` | ❌ 不要找 `muse.py`（不存在，2026-09-21 核实） |
| 命令清单与示例 | `skill/tools/cli-commands.md` | ❌ 不要拿 `cli.py doctor` 当状态查询 —— 它会发一次真实 embedding 推理（写操作级别） |
| **模型在线吗**（只读，不触发加载） | `python skill/scripts/status.py`，或 `curl 127.0.0.1:9123/running` | ❌ 不要 POST；不要为了"看看"触发模型加载 |
| 显存账 / 配置级陷阱长文 | `skill/references/full-workflow.md` | — |
| 架构决策与理由（含已否决方案） | `skill/rules/decisions.md` | — |
| 提示词正文 | `skill/system/*-prompt.md` | ❌ 不要在 Python 代码里硬编码 prompt |
| 项目介绍 / 安装使用 | `README.md`（给人看，不写规则） | — |

## 功能指针（要改东西 / 查规则时读）

- 真相源：skill/AGENTS.md（skill 操作细则 + 完整真相源分配表）
- 真相源：skill/rules/redlines.md（红线，**唯一出处**）
- 真相源：skill/rules/registry.md（模型台账：清单 / 端点 / 维度）
- 真相源：skill/rules/troubleshooting.md（排障）
- 真相源：skill/references/full-workflow.md（配置级陷阱与性能长文）
- 真相源：skill/tools/cli-commands.md（命令清单）
- 真相源：skill/system/pdf-vlm-prompt.md（提示词正文，禁止硬编码进代码）

## 日常命令

```powershell
pip install -r skill/requirements.txt
python skill/scripts/cli.py --help
ruff check skill/scripts skill/tests    # CI（.github/workflows/test.yml）会跑
pytest skill/tests -q                   # 同上；integration 用例离线自动跳过
```

## 边界（防漂移）

本文件**只存指针，不复制真相源内容**。发现本文件与 `skill/` 下的文档冲突 = 导出漂移，
以母库为准。**不要在本仓直接改真身** —— 改动会在下一次导出时被冲掉；要改就改母库再导出。
