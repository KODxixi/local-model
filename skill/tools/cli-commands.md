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

# 查询扩展（提升召回率）
cli.py --kb <name> retrieve "查询" --expand

# 多轮上下文消歧
cli.py --kb <name> retrieve "查询" --context "上一轮对话"

# MMR 多样性重排
cli.py --kb <name> retrieve "查询" --mmr

# 自动路由（选 semantic/keyword/hybrid）
cli.py --kb <name> retrieve "查询" --auto-route

# Parent-Child 检索
cli.py --kb <name> retrieve "查询" --parent-child
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

### optimize
优化索引（重建 FTS 索引等）。

```bash
cli.py --kb <name> optimize
```

### migrate
旧 SQLite 索引迁移到 LanceDB。

```bash
cli.py --kb <name> migrate
```

### stats
查看知识库统计。

```bash
cli.py --kb <name> stats
```

## 知识图谱命令

### kg extract
从已索引内容提取知识图谱。

```bash
cli.py --kb <name> kg extract
```

### kg find
按实体/关系查询知识图谱。

```bash
cli.py --kb <name> kg find "实体名"
```

### kg related
查相关实体。

```bash
cli.py --kb <name> kg related "实体名"
```

### kg list
列出所有实体。

```bash
cli.py --kb <name> kg list
```

### kg stats
知识图谱统计。

```bash
cli.py --kb <name> kg stats
```

## Muse Glimmer 管理

### muse status
查看 30B 状态。

```bash
python scripts/muse.py status
```

### muse start
启动 30B。

```bash
python scripts/muse.py start
```

### muse stop
停止 30B。

```bash
python scripts/muse.py stop
```
