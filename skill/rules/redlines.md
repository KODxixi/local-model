# 红线规则

> 必须遵守，违反即 bug。

## 端点隔离

1. **embed/rerank 只走已配置的本地 llama-swap**，不得用外部模型兜底；端点以 `C:\AI\tools\llama-swap\config.yaml` 为准。
2. 统一调度端点下仍按模型 id 区分向量、重排与对话/视觉职责，不能拿对话模型处理 embedding 请求。
3. 检索不通时先 `curl 127.0.0.1:9123/running`，绝不靠改端点到别处应急

## 数据边界

4. 索引是**可再生投影**，真相源是原始文件；永不反向写
5. 不读取、输出或改写凭据；不扫描 private 目录

## 显存优先级

6. **显存预算**：检索栈已能与 30B 同时常驻。
   两类向量互斥（`exclusive: true`）是**设计**，不是妥协；reranker 组必须 `persistent: true`。
   **余量的具体数字以 `C:\AI\memory\_canonical\LOCAL-MODELS.md` **第四节** 为唯一权威 —— 本文件不复述**（复述必漂）。
   结论只有一条：常驻稳态余量很薄，**不得再往这张卡上叠第三个吃显存的程序**。
   动 `-c` / `-b` 之前先读那一段（注意它给的是**场景 + 区间**，不是一个孤立数字）。
7. 遇到显存冲突（OOM、或要临时加载未登记/已移除的模型）：**不要自行调度、不要降级硬跑**——
   立即通知用户，说明谁在占显存、要起什么、预计占用，由用户决定启停顺序

## 配置真相源

8. 服务模型、地址、启动与生命周期以 `C:\AI\tools\llama-swap\config.yaml` 为准；Skill 知识库模板在 `registry.yaml`，不得混淆两者。
9. 本机知识库路径写 `registry.local.yaml`（已 gitignore），由 `load_registry()` 合并；当前加载与覆盖约定见 [registry.md](registry.md)，不在代码硬编码本机路径。
