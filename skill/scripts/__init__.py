r"""local-model Skill 公共 API。

外部项目可直接复用 embedding/rerank 客户端和知识库注册表，
避免重复实现模型配置、重试逻辑、降级链。

用法（scripts/ 是普通目录，**先加 sys.path 再按模块名 import**）:
    import sys
    sys.path.insert(0, "<skill_root>/scripts")

    from rag_client import embed_texts, embed_images, rerank_texts
    from rag_indexer import load_registry

    # 加载注册表（唯一真相源；有 registry.local.yaml 时自动合并本机私有库）
    kbs = load_registry("<skill_root>/registry.yaml")
    kb = kbs["my_docs"]

    # 文本嵌入（带指数退避重试，适配 llama-swap TTL 冷加载）
    vectors, dims = embed_texts(["hello world"], include_dimensions=True)

    # 图片嵌入（2048维，单图失败隔离，批量请求）
    img_vectors = embed_images([r"path/to/photo.png"])

    # 重排
    results = rerank_texts("query", ["doc1", "doc2"])

注意:
- rag_client 模块不依赖 lancedb，始终可导入
- rag_indexer / vector_store 依赖 lancedb/pyarrow，缺失时导入失败但不影响 rag_client
- 本文件导出的 DEFAULT_REGISTRY_PATH 是给外部包按 `import scripts` 使用时的便捷常量
"""

from __future__ import annotations

from pathlib import Path

# 默认注册表路径（相对于 skill 根目录）
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "registry.yaml"

# --- rag_client: 不依赖 lancedb，始终可用 ---
from rag_client import (
    DEFAULT_TEXT_EMBED_MODEL,
    DEFAULT_TEXT_RERANK_MODEL,
    DEFAULT_VL_EMBED_MODEL,
    DEFAULT_VL_RERANK_MODEL,
    embed_images,
    embed_texts,
    health_check,
    rerank_texts,
)

# --- rag_indexer / vector_store: 依赖 lancedb，缺失时不导出 ---
try:
    from rag_indexer import KBConfig, RAGIndexer, load_registry
except ImportError:
    KBConfig = None  # type: ignore[assignment,misc]
    RAGIndexer = None  # type: ignore[assignment,misc]

    def load_registry(registry_path: str | Path) -> dict:  # type: ignore[misc]
        """加载知识库注册表（依赖 lancedb 缺失时的 fallback）。

        同样会合并同目录的 registry.local.yaml（本机私有配置）。
        """
        import yaml
        path = Path(registry_path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        local_path = path.with_name(f"{path.stem}.local{path.suffix}")
        if local_path.exists():
            local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
            data["knowledge_bases"] = {
                **(data.get("knowledge_bases") or {}),
                **(local.get("knowledge_bases") or {}),
            }
        return data.get("knowledge_bases") or {}

try:
    from vector_store import (
        ImageStoreProtocol,
        LanceDBVectorStore,
        VectorStore,
        VectorStoreProtocol,
        create_vector_store,
    )
except ImportError:
    VectorStore = None  # type: ignore[assignment,misc]
    LanceDBVectorStore = None  # type: ignore[assignment,misc]
    VectorStoreProtocol = None  # type: ignore[assignment,misc]
    ImageStoreProtocol = None  # type: ignore[assignment,misc]
    create_vector_store = None  # type: ignore[assignment,misc]

__all__ = [
    # 配置
    "DEFAULT_REGISTRY_PATH",
    # 注册表
    "load_registry",
    "KBConfig",
    # 嵌入/重排
    "embed_texts",
    "embed_images",
    "rerank_texts",
    "health_check",
    # 模型常量
    "DEFAULT_TEXT_EMBED_MODEL",
    "DEFAULT_TEXT_RERANK_MODEL",
    "DEFAULT_VL_EMBED_MODEL",
    "DEFAULT_VL_RERANK_MODEL",
    # 索引/存储（可能为 None）
    "RAGIndexer",
    "VectorStore",
    "VectorStoreProtocol",
    "ImageStoreProtocol",
    "LanceDBVectorStore",
    "create_vector_store",
]
