# CLI 命令清单

> 真相源：`scripts/cli.py`

## 全局参数

| 参数 | 说明 |
|------|------|
| `--kb <name>` | 知识库名称（必填，写在子命令之前） |
| `--db <path>` | 自定义数据库路径 |
| `--registry <path>` | 自定义注册表路径 |
| `--json` | JSON 格式输出 |

> ⚠️ 全局参数必须写在子命令**之前**。

## 核心命令

### doctor
诊断系统状态（不需要 `--kb`）。

```bash
cli.py doctor
```

### index
建索引（增量）。

```bash
# 增量索引
cli.py --kb <name> index

# 全量重建
cli.py --kb <name> index --force
```

### retrieve
检索。

```bash
# 文本检索（默认 hybrid+智能权重）
cli.py --kb <name> retrieve "查询内容"

# 跨库检索
cli.py --kb all retrieve "查询内容"

# ⚠️ 上面这一整块（查询扩展 / 上下文消歧 / MMR / 自动路由 / Parent-Child）**全部已不存在**：
#    --expand / --mmr / --route / --parent-child 在 CLI 里 0 次命中；
#    --context 也不在 retrieve 上（它挂 rewrite）。
#    现有诊断 flag 见本节上方 retrieve 的真实清单（--explain / --trace / --no-rerank / --path-filter）。
```

### ingest
解析文件为结构化文档。

```bash
cli.py ingest <文件路径> --json
```

### rewrite
查询改写。

```bash
cli.py rewrite "原始查询"
```

### summary
文档摘要。

```bash
cli.py summary <文件路径>
```

## 工具命令

### search-image
以图搜图。

```bash
cli.py --kb <name> search-image <图片路径>
```

### freshness
检查新鲜度。

```bash
cli.py --kb <name> freshness
```

### optimize（注意：是 `vector_store.py` 的子命令，不是 `cli.py` 的）
优化索引（重建 FTS 索引等）。

```bash
python scripts/vector_store.py optimize --db <db> --kb <kb>
```

### migrate（同上，`vector_store.py` 的子命令）
旧 SQLite 索引迁移到 LanceDB。

```bash
python scripts/vector_store.py migrate --sqlite <path> --db <db> --kb <kb>
```

### stats
查看知识库统计。

```bash
cli.py --kb <name> stats
```

## 已砍掉的命令（不要再教）

> **`kg extract/find/related/list/stats/visualize` 已从 CLI 移除**（2026-09-21 核实。
> `AGENTS.md` 的负面指针也确认「kg 命令已砍掉」）。
> `index --extract-entities` 仍会把实体抽出来，但**读取端不存在** —— 只写不读。

要诊断检索为什么没命中，用 `retrieve` 的真实 flag：

```bash
cli.py --kb <name> retrieve "查询" --explain          # 每条的四路分数（语义/关键词/RRF/rerank）
cli.py --kb <name> retrieve "查询" --trace out.html   # 检索推理链可视化
cli.py --kb <name> retrieve "查询" --no-rerank         # 跳过精排，只看召回
```

## Muse Glimmer 管理

> ⚠️ **不存在 `muse.py`**（2026-09-21 核实）。此前本文件教的 `muse status/start/stop`
> 指向一个早已删除的脚本，照跑必然 `No such file`。

真实管理方式（完整契约见 `C:\AI\memory\_canonical\LOCAL-MODELS.md` 第三节）：

```powershell
# 2026-09-21 甲-1：30B 由 llama-swap 托管，**不再手工起独立实例**。
# 首个请求自动 spawn（冷加载实测 ~54s）；ttl: 900 空闲自卸。

# 查活体（只读，不触发加载）
curl http://127.0.0.1:9123/running                          # 已加载项 + state + ttl
curl http://127.0.0.1:9123/upstream/muse-glimmer-30b/health # {"status":"ok"}

# 卸载 / 改完 config.yaml 后重载
curl -X POST http://127.0.0.1:9123/api/models/unload/muse-glimmer-30b
schtasks /End /TN LlamaSwap; schtasks /Run /TN LlamaSwap

# ⚠️ 计划任务 MuseGlimmer 已 Disabled —— 启用会起第二个 30B，直接 OOM
```