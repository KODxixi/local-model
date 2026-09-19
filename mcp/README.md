# local-models MCP

本地模型 **stdio 适配层（薄入口）**。本层**不实现业务**，只把 Skill 层（`<仓库根>/skill`）的 RAG / embed / rerank / 模型管理能力包成 MCP 工具，供其他 agent 框架通过 stdio 跨语言调用。

> **2026-09-16 P2 彻底瘦身**：MCP 层从"自带 embed/rerank HTTP + SQLite 索引"彻底收敛为纯入口。文本 embed/rerank 的类包装（`LMStudioEmbedder` / `RerankerClient`）已删除，`backends/reranker.py` 整文件删除；`server.py` 只做参数校验 + 调 Skill 层 + 格式化输出。

## 与 Skill 的关系

```
其他 agent 框架 ──stdio──> mcp/server.py（本层，薄适配）
                              │  sys.path.insert Skill scripts
                              ▼
                  <仓库根>/skill/scripts（唯一实现）
                  rag_client / rag_indexer / rag_retriever / vector_store
```

| 层 | 位置 | 职责 |
|---|---|---|
| **Skill（实现层）** | `<仓库根>/skill` | 文件解析、分块、embed/rerank HTTP（重试/降级）、LanceDB 索引、检索编排、查询改写、知识图谱 |
| **MCP（入口层）** | 本目录 | stdio 协议 + 参数校验 + 调用 Skill 公共 API + JSON 输出 + 错误转换 |

**唯一真相源配置**：`<仓库根>/skill/registry.yaml`。`kbs.yaml` 已 deprecated，仅作 fallback。

## 工具清单 → Skill 层 API 映射

| MCP 工具 | 委托的 Skill 层 API | 说明 |
|---|---|---|
| `list_kbs()` | `load_registry(registry.yaml)` | 列出知识库与能力 |
| `search(kb, query, top_k, image, mode)` | `RAGRetriever(kb).retrieve(...)` | 文本库混合检索；图文库走外部图文库 CLI |
| `index(kb)` | `RAGIndexer(kb).index(force=False)` | 文本库建/刷新 LanceDB 索引 |
| `embed(text, image)` | `rag_client.embed_texts()` / `image_retrieval` 多模态 | 文本走文本 embedding；图片走图文 embedding |
| `rerank(kb, query, candidates, top_k)` | `rag_client.rerank_texts()` | 文本库走文本 rerank；图文库走图文 rerank |
| `ocr(image_or_pdf, use_vlm)` | 外部 OCR 脚本（独立 venv） | 独立保留（Skill 层无对应）。路径用 `LOCAL_MODEL_OCR_PYTHON` / `LOCAL_MODEL_OCR_SCRIPT` 指定 |
| `status()` | `rag_client.embed_texts/rerank_texts` 探针 + `image_retrieval.health` | 健康检查 |
| `list_models()` / `load_model()` / `unload_model()` | `backends/lmstudio.py` lms CLI | 对话模型装卸（Skill 层无管理 API） |

## backends/ 目录（只保留 Skill 层没有的异构适配）

| 文件 | 保留原因 |
|---|---|
| `image_retrieval.py` | 图文 HTTP 适配器（多模态 embed/rerank/health），Skill 层无对应物 |
| `_http.py` | 共享指数退避重试，image_retrieval.py 依赖 |
| `lmstudio.py` | lms CLI 模型管理（list/load/unload）+ `_request_json` re-export |
| ~~`reranker.py`~~ | **已删除**——文本 rerank 直接调 `rag_client.rerank_texts` |
| ~~`LMStudioEmbedder`~~ | **已删除**——文本 embed 直接调 `rag_client.embed_texts` |
| `indexer.py` | **DEPRECATED**——旧 SQLite 索引器，已被 Skill 层 `rag_indexer.py`（LanceDB）取代，server.py 不引用 |

## 后端与红线

- **检索模型**：llama-swap `127.0.0.1:9123`（embedding + rerank，TTL 自动装卸）
- **对话/视觉模型**：LM Studio `127.0.0.1:1234`
- **图文检索**：外部图文库 CLI（LanceDB，embed/rerank 走检索端点）

红线（server.py 启动硬校验，违反即启动失败）：
1. embed/rerank **永远只走 llama-swap 9123**；LM Studio 1234 **只跑对话/识图**
2. 禁止把 `text-embedding-*` / `*-reranker-*` load 进 LM Studio
3. 检索不通时先 `curl 127.0.0.1:9123/running`，绝不靠改端点到 LM Studio 应急

## 安装依赖

```powershell
python -m pip install -r mcp\requirements.txt
```

`server.py` 用的是 mcp v1 的 `FastMCP` API，所以 requirements 里锁 `mcp<2`。
升到 2.x 需要按官方迁移指南把它换成 `MCPServer`（接口有变），目前未迁移。

## 注册到 agent

```json
{
  "mcpServers": {
    "local-models": {
      "type": "stdio",
      "command": "python",
      "args": ["<仓库根>\\mcp\\server.py"]
    }
  }
}
```

Skill 层路径默认按仓库布局自动定位（`<仓库根>/skill/scripts`），
可用环境变量 `LOCAL_MODEL_SKILL_PATH` 覆盖；注册表路径用 `LOCAL_MODEL_REGISTRY_PATH` 覆盖。
