# 故障排查

> 常见错误与解决方案

## 连接类错误

| 现象 | 排查步骤 |
|------|----------|
| `ConnectionReset WinError 10054` | 检索端点模型 TTL 卸载，冷加载 ~12s。rag_client 内置 5 次指数退避重试，通常第 2-3 次成功。持续失败检查 `curl 127.0.0.1:9123/running` |
| 对话/视觉调用失败 | 只读查两条：`curl 127.0.0.1:9123/running`（`muse-glimmer-30b` 在不在列、`state` 是什么）+ `curl 127.0.0.1:9123/upstream/muse-glimmer-30b/health`（`{\"status\":\"ok\"}`）。未加载时**首个请求会自动 spawn**，冷加载实测 **~54s**，期间请求会等着。服务本身由 `schtasks /Query /TN LlamaSwap` 管 —— 别再找 8080（甲-1 已退役） |

## 依赖类错误

| 现象 | 排查步骤 |
|------|----------|
| `ModuleNotFoundError: lancedb` | 依赖未安装。运行 `setup.ps1`，或用 venv Python |
| `ModuleNotFoundError: markitdown` | 同上，markitdown 是可选依赖，用于 docx/xlsx/pptx 解析 |

## 数据类错误

| 现象 | 排查步骤 |
|------|----------|
| `向量维度不匹配` | 降级链切换了模型。检查 `registry.yaml` 的 `embed_model` 和 `dimensions` 是否一致 |
| 单张图片嵌入返回零向量 | 图片损坏/格式不支持，已被单图失败隔离。用 `all(v==0 for v in vector)` 检测并过滤 |
| 关键词检索返回空 | 确认 LanceDB 表有 INVERTED FTS 索引。跑 `cli.py --kb <name> index --force` 重建索引（`optimize` 子命令不存在） |

## 命令类错误

| 现象 | 排查步骤 |
|------|----------|
| `E_INVALID_ARGS ... 需要 --kb` | 没写 `--kb`。用 `cli.py doctor` 看已注册的库 |
| doctor 命令报错 | doctor 不依赖 lancedb，应始终可用。如 import 阶段崩溃，检查 `cli.py` 顶部延迟导入是否被破坏 |
| `另一个索引进程正在运行 (PID=…)` | 索引有 PID 文件锁；确认无进程后删 `~/.local-rag/index.lock` |

## 检索质量问题

| 现象 | 解决方案 |
|------|----------|
| 语义检索结果排序异常 | 确认用的是最新代码（L2距离越低越相似，已修复排序方向） |
| 代词查询（"它"/"这个"）结果差 | 把代词替换成实体名再检索。⚠️ `retrieve` **没有** `--context`（它挂在 `rewrite` 上，2026-09-21 实测 `cli.py retrieve --help`） |

> **已砍掉的参数，别再教人用**（2026-09-21 独立审计查出）：
> `--mmr`（多样性重排）与 `--expand`（LLM 查询扩展）**在 CLI 里已不存在**，
> 只剩 `--context`（`cli.py:829`）。`AGENTS.md` 的负面指针也确认「MMR/parent-child/
> 多查询扩展已砍掉」。底层函数（`rag_retriever.py` 的 `_mmr_reorder` / `_expand_query`）
> 成了死代码 —— 谁拿这两个 flag 去跑，必然 `unrecognized arguments`。
