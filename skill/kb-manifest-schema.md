# 可插拔垂类库包（Vertical Package）规范

> 配套 local-model SKILL「可插拔垂类库」节。定义"一个垂类 = 一个自包含、可挂载/卸载的
> 知识/数据组件"的清单(schema)与治理规则。落地前先核下方「现有代码约束」。

## 1. 目标与铁律

1. **一个垂类 = 一个包**：真源指针 + manifest + 专属投影/后端 + 生命周期命令，打包在一起。
   例：图库（多模态案例）、城市数据（结构化事实）、资讯笔记（同源文本子域）。
2. **独立性定义在"投影 + 生命周期"层**，不是"每库一份 sqlite 拷贝"。
   - 同源文本 → 共享检索引擎 + 分区视图（path_filter），**禁止同一语料被嵌入两遍**。
   - 异质/图文/结构化/超大 → 专属后端（own LanceDB / own DB / 工具槽位）。
3. **挂载/卸载 = 注册表增删一条记录**。卸载不删真源、不删已建投影；仅从 agent 可见目录消失。
4. **单向只读**：真源 → 投影；投影可再生（PATHS.md L1-5）；agent 永不反向写。
5. **agent 自行判定调用**：agent 先读目录(发现性元数据)再选槽位，绝不"搜遍所有库"。

## 2. 包结构（物理形态，任一位置可放）

```
verticals/<name>/
├── manifest.yaml          # 本规范 §3：包自描述（唯一必读文件）
├── truth/                 # 可选：真源指针或软链；也可指向现有真相源目录
└── projection/            # 可选：本包专属投影（text 引擎 root 分区 / LanceDB / DB）
```

真源通常**不搬动**——包只声明"我的语料是哪个目录/哪个系统"，检索投影才是包自带的。

## 3. manifest schema

字段全部为 agent 路由所需（list 时公开）或生命周期所需。`name` 全局唯一。

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | slug，全局唯一，也是检索/工具槽位名 |
| `title` | ✅ | 人读标题 |
| `category` | ✅ | 域标签：realestate / scheme / ai-info / finance / …（agent 路由用） |
| `kind` | ✅ | 槽位类型：`semantic`(语义检索) / `data`(事实查询工具) / `files`(文件归档检索) |
| `slot` | ✅ | semantic→`search(kb=<name>)` 用的 kb 名；data→适配器 id（akshare/datapro/local-db/…） |
| `truth_source` | ✅ | 真源绝对路径 或 系统引用；卸载时不删 |
| `semantic` | kind=semantic 时 | 见下表 |
| `data` | kind=data 时 | 适配器与连接/范围说明 |
| `capability` | ✅ | `good_for` / `not_for` / `example_queries[]` / `freshness` —— agent 判定调用的核心依据 |
| `lifecycle` | ✅ | build / sync / prune / full_rebuild / cost_note |
| `registration` | ✅ | 注册点：`registry.yaml` 条目 或 数据工具注册表 |

`semantic` 子块：

| 字段 | 必填 | 说明 |
|---|---|---|
| `engine` | ✅ | `shared-text`（共享 LanceDB 文本引擎，表 `kb_{name}`）或 `multimodal`(own LanceDB) |
| `type` | | `text` / `multimodal` |
| `root` | ✅ | 真实语料根（绝对路径） |
| `patterns` | | 文本引擎文件后缀；multimodal 填图文库根 |
| `path_filter` | | **同 root 复用父索引时的分区**（不建独立 chunk 集） |
| `embed` | | model + dimensions（默认 text 4096 / multimodal 2048） |

`data` 子块（事实/结构化，**不进 embedding**）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `adapter` | ✅ | akshare / datapro / local-sqlite / 自有查询层 |
| `scope` | ✅ | 覆盖哪些数据（城市/标的/表） |
| `conn` | | 连接引用（文件路径 / 凭据键——凭据只在 private/secrets） |

## 4. 后端分档（决定"共享还是独立"）

| 档 | 语料形态 | 该用什么 | 铁律 |
|---|---|---|---|
| T0 | 结构化事实（行情/成交/土地指标） | **数据槽位**（akshare/datapro/本地 DB），非 KB | 事实永不走语义嵌入 |
| T1 | 同 root 下的子域文本笔记 | shared-text + `path_filter` 分区视图 | 不重复建索引 |
| T2 | 独立 root 的纯文本语料 | shared-text，按 `root` 键隔离的 chunk 分区 | 若需硬隔离→独立 index 实例 |
| T3 | 图文/超大/异构 | **专属 multimodal 包**（own LanceDB + VL embed） | 绝不全量 inline 嵌入 |

**同源重复嵌入 = 架构罪**。新增语义包前先查语料是否已被别的包覆盖；是→加视图，否→才建新分区/后端。

## 5. agent 调用协议（如何判断"何时/如何调用"）

1. **先分事实 vs 语义**：要精确数字/状态 → 查 `data` 槽位（akshare/datapro/DB）；要读观点/案例/资料 → 走 `semantic`。
2. **读目录再选**：`list_kbs`（+能力描述）挑 `category` 匹配、`capability.good_for` 覆盖本问的包，传 `slot`。
3. **拿不准 → 收窄**：≤2 个候选各 `search(kb=…)` 一次，用命中质量定答案源；兜底 = 覆盖面最广的那个文本库。
4. **结构化结论携带源**：命中返回 `source_path` → agent 回真源 Read 原文核对，杜绝黑箱。
5. 包给出 `capability.not_for` 时，agent 不得硬用（防跨域污染）。

## 6. 生命周期

| 动作 | semantic(shared-text) | semantic(multimodal) | data |
|---|---|---|---|
| 建/全量 | `index(kb)` | 外部 CLI 建索引 | 建表/灌数脚本 |
| 增/改 | `index(kb)`（mtime 增量） | ingest + build | 增量脚本 |
| 删(源文件) | ✓ `index(kb)` 自动清孤儿（仅全量扫描时，见 §7） | 删源 + 重 build（重） | 删行 |
| 卸载 | 注册表移除记录；源/投影保留 | 同左 | 同左 |

无 watcher：增删真相源后需手动触发该包 `lifecycle.build/sync`，或挂 cron。

## 7. 现有代码约束（落地前必须知晓 / 待补缺口）

- 文本引擎是 LanceDB（`~/.local-rag/lancedb/`），每库一张 `kb_{name}` 表（4096 维）+ 可选 `kb_{name}_images` 表（2048 维）；
  chunk 键 `(chunk_id, path)`，**同 root 只能有一份 chunk 集** → 子域一律 path_filter 视图，别同 root 叠两个包。
  旧 SQLite 索引（`~/.claude/mcp/local-models/index.sqlite3`）已废弃，用 `cli.py migrate` 迁移。
- `indexer.index()` 上限：`max_files=10000` / 单文件 `max_file_bytes=5_000_000`（5MB，registry.yaml 默认；超限需拆 root 或改配置）。
- **孤儿清理（2026-09-02 已实现）**：`index()` 默认按 root 删除真源已消失的孤儿 chunk；
  **仅全量扫描（未截断）时执行**——文件数 >10000 时自动停用（返回 `prune_skipped_truncated`），防误删超限未扫文件。
- 剩余缺口（单文件 5MB 上限、prune 手动触发等）属本 skill 的改动。

## 8. 三类垂类落位样例

| 垂类 | 真源 | kind / 槽位 | 引擎 |
|---|---|---|---|
| 城市结构化数据 | `~/data/city`（分城市）+ 笔记库 | data(外部数据工具) + semantic(`city-notes`) | T0 + T1 视图 |
| 案例图库 | `~/knowledge/image-cases` | semantic(`image-cases`) | T3 multimodal（own LanceDB） |
| 资讯笔记 | 共享文本库的子目录 | semantic(`ai-info` 视图) | T1 path_filter |

完整 manifest 示例见下方附录。

---

## 附录 A：图文库 manifest 示例（T3 自打包）

```yaml
name: image-cases
title: 图文案例库
category: scheme
kind: semantic
slot: image-cases
truth_source: ~/knowledge/image-cases
semantic:
  engine: multimodal
  root: ~/knowledge/image-cases
  embed: { model: vl-embedding-2b, dimensions: 2048 }
  rerank: { model: vl-reranker-2b }
  projection: ~/knowledge/image-cases/.index/lancedb
capability:
  good_for: 按效果图/图纸找案例、以图搜图、立面/类型/风格检索
  not_for: 精确数字、文本长篇语义问答
  example_queries: ["办公楼 玻璃幕墙", "以图搜图"]
  freshness: 全量 build 驱动
lifecycle: { build: "外部 CLI 建索引", sync: "ingest+build", prune: "重 build", cost: "源大时按批次跑" }
registration: registry.yaml
```

## 附录 B：结构化数据槽示例

```yaml
name: city-data
title: 城市结构化数据
category: realestate
kind: data
slot: local-db
truth_source: ~/data/city
data:
  adapter: local-db            # 或外部数据源适配器
  scope: 成交·价格·土地
  conn: <待建，sqlite 或 adapter 引用>
capability:
  good_for: "某城市某月成交均价、环比、挂牌/成交结构"
  not_for: 语义问答、主观解读
  example_queries: ["某城市 2026 成交均价"]
  freshness: 随数据更新灌库
lifecycle: { build: "入库脚本", sync: "增量", prune: "按表清理" }
registration: 数据工具注册表（不进 registry.yaml 语义库）
```

> 附注：结构化事实（行情/成交/指标）走 data 槽位的适配器，**永不进 embedding**；
> 与之相关的解读性文字才进语义库。

