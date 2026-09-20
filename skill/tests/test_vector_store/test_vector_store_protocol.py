"""P0: VectorStore 抽象层契约测试。

验证：
1. LanceDBVectorStore 满足 VectorStoreProtocol（runtime_checkable isinstance）
2. create_vector_store 工厂按 backend 分发
3. 不支持的 backend 抛 ValueError
4. LanceDBVectorStore 具备 Protocol 定义的全部方法
5. VectorStore 别名向后兼容（VectorStore is LanceDBVectorStore）

运行：python -m pytest tests/test_vector_store_protocol.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from vector_store import (  # noqa: E402
    LanceDBVectorStore,
    VectorStore,
    VectorStoreProtocol,
    create_vector_store,
)

# Protocol 声明的全部方法名（与 vector_store.py 中 VectorStoreProtocol 保持一致）
PROTOCOL_METHODS = [
    "upsert_chunks",
    "search",
    "delete_by_path",
    "stats",
    "delete_orphans",
    "optimize",
    "close",
]


def test_lancedb_implements_protocol(tmp_path):
    """isinstance(LanceDBVectorStore(...), VectorStoreProtocol) 为 True。"""
    store = LanceDBVectorStore(tmp_path, "proto_check", dimensions=4, model="test")
    assert isinstance(store, VectorStoreProtocol)
    store.close()


def test_factory_creates_lancedb(tmp_path):
    """create_vector_store(backend="lancedb") 返回 LanceDBVectorStore。"""
    store = create_vector_store(
        backend="lancedb",
        db_path=str(tmp_path),
        kb_name="factory_lance",
        dimensions=4,
        model="test",
    )
    assert isinstance(store, LanceDBVectorStore)
    assert isinstance(store, VectorStoreProtocol)
    store.close()


def test_factory_unsupported_backend_raises():
    """create_vector_store(backend="qdrant") 抛 ValueError。"""
    try:
        create_vector_store(backend="qdrant", db_path="/tmp/should-not-create")
    except ValueError as exc:
        assert "qdrant" in str(exc)
        return
    raise AssertionError("create_vector_store(backend='qdrant') 应抛 ValueError")


def test_protocol_methods_exist():
    """LanceDBVectorStore 具备 VectorStoreProtocol 定义的所有方法。"""
    for name in PROTOCOL_METHODS:
        assert callable(getattr(LanceDBVectorStore, name, None)), (
            f"LanceDBVectorStore 缺少 Protocol 方法 {name!r}"
        )


def test_backward_compatibility_alias():
    """VectorStore is LanceDBVectorStore 为 True（向后兼容）。"""
    assert VectorStore is LanceDBVectorStore
