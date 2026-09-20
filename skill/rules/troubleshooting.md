# 故障排查

> 常见错误与解决方案

## 连接类错误

| 现象 | 排查步骤 |
|------|----------|
| `ConnectionReset WinError 10054` | 检索端点模型 TTL 卸载，冷加载 ~12s。rag_client 内置 5 次指数退避重试，通常第 2-3 次成功。持续失败检查 `curl 127.0.0.1:9123/running` |
| 对话/视觉端点连接失败 | 检查 Muse Glimmer 是否启动。用 `muse.py status` 查看，`muse.py start` 启动 |

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
| 关键词检索返回空 | 确认 LanceDB 表有 INVERTED FTS 索引。运行 `cli.py --kb <name> optimize` 重建索引 |

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
| 检索结果高度相似 | 加 `--mmr` 参数启用多样性重排 |
| 短查询召回率低 | 加 `--expand` 参数启用查询扩展（LLM 生成同义改写） |
| 代词查询（"它"/"这个"）结果差 | 加 `--context "上一轮对话"` 启用上下文消歧 |
