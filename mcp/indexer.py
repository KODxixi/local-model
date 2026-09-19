"""SQLite-backed text index. Migrated from the retired qwen-embedding MCP.

DEPRECATED (2026-09-16, P2 MCP 彻底瘦身):
    已被 Skill 层 ``scripts/rag_indexer.py``（LanceDB ANN）取代。
    server.py 不再 import 本模块，仅作历史参考保留。
    新代码/新索引一律走 Skill 层 RAGIndexer，不要使用本文件的 SQLite Indexer。

The chunks table format is preserved so an existing SQLite index is reused
as-is; no re-embedding is required on migration.
"""

from __future__ import annotations

import fnmatch
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any, Callable

EmbedFn = Callable[[list[str]], list[list[float]]]

DEFAULT_PATTERNS = ["*.md", "*.txt", "*.py", "*.js", "*.ts", "*.tsx", "*.jsx", "*.json", "*.yaml", "*.yml"]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _cosine(left: list[float] | tuple[float, ...], right: list[float] | tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(v * v for v in left))
    right_norm = math.sqrt(sum(v * v for v in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _matches(path: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name.lower(), pattern.lower()) for pattern in patterns)


def _chunks(text: str, max_chars: int = 1800, overlap_lines: int = 4) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    results: list[tuple[int, int, str]] = []
    start = 0
    while start < len(lines):
        end = start
        chars = 0
        while end < len(lines) and (chars + len(lines[end]) + 1 <= max_chars or end == start):
            chars += len(lines[end]) + 1
            end += 1
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            results.append((start + 1, end, chunk))
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap_lines)
    return results


class Indexer:
    """Persistent text index keyed by (root, model)."""

    def __init__(self, db_path: str | Path, model: str) -> None:
        self.db_path = Path(db_path)
        self.model = model

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                root TEXT NOT NULL,
                path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                model TEXT NOT NULL,
                PRIMARY KEY (path, start_line, model)
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_root_model ON chunks(root, model)")
        return connection

    def index(
        self,
        root: str | Path,
        patterns: list[str],
        embed: EmbedFn,
        max_files: int = 3000,
        max_file_bytes: int = 2_000_000,
        prune: bool = True,
    ) -> dict[str, Any]:
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise ValueError(f"目录不存在: {root_path}")
        discovered = [
            path for path in root_path.rglob("*")
            if path.is_file() and _matches(path, patterns) and path.stat().st_size <= max_file_bytes
        ]
        truncated = len(discovered) > max_files
        files = discovered[:max_files]

        indexed_files = 0
        indexed_chunks = 0
        skipped_files = 0
        with self._connect() as connection:
            for path in files:
                stat = path.stat()
                existing = connection.execute(
                    "SELECT 1 FROM chunks WHERE path = ? AND mtime_ns = ? AND model = ? LIMIT 1",
                    (str(path), stat.st_mtime_ns, self.model),
                ).fetchone()
                if existing:
                    skipped_files += 1
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                chunks = _chunks(text)
                if not chunks:
                    continue
                connection.execute("DELETE FROM chunks WHERE path = ?", (str(path),))
                for offset in range(0, len(chunks), 16):
                    batch = chunks[offset : offset + 16]
                    vectors = embed([item[2] for item in batch])
                    for (start_line, end_line, chunk), vector in zip(batch, vectors):
                        connection.execute(
                            "INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                str(root_path), str(path), stat.st_mtime_ns, start_line, end_line,
                                chunk, _pack(vector), len(vector), self.model,
                            ),
                        )
                        indexed_chunks += 1
                connection.commit()
                indexed_files += 1

        # 孤儿清理：仅当本次全量扫描（未因 max_files 截断）时执行，否则会把超限未扫的文件误当孤儿删除
        pruned_rows = 0
        pruned_files = 0
        if prune and not truncated:
            stored_paths = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT path FROM chunks WHERE root = ? AND model = ?",
                    (str(root_path), self.model),
                )
            ]
            present = {str(path) for path in discovered}
            for path in stored_paths:
                if path not in present:
                    cur = connection.execute(
                        "DELETE FROM chunks WHERE root = ? AND path = ? AND model = ?",
                        (str(root_path), path, self.model),
                    )
                    pruned_rows += cur.rowcount or 0
                    pruned_files += 1
            if pruned_rows:
                connection.commit()

        return {
            "root": str(root_path),
            "model": self.model,
            "files_seen": len(files),
            "files_indexed": indexed_files,
            "files_unchanged": skipped_files,
            "chunks_indexed": indexed_chunks,
            "chunks_pruned_rows": pruned_rows,
            "files_pruned": pruned_files,
            "prune_skipped_truncated": bool(truncated and prune),
        }

    def recall(
        self,
        root: str | Path,
        query_vector: list[float],
        recall: int = 20,
        path_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        root_path = str(Path(root).expanduser().resolve())
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT path, start_line, end_line, text, embedding FROM chunks WHERE root = ? AND model = ?",
                (root_path, self.model),
            ).fetchall()
        if not rows:
            raise ValueError(f"{root_path} 尚无索引，请先 index(kb)")
        results = [
            {
                "path": row[0],
                "start_line": row[1],
                "end_line": row[2],
                "score": round(_cosine(query_vector, _unpack(row[4])), 6),
                "snippet": row[3][:1200],
                "_text": row[3],
            }
            for row in rows
        ]
        if path_filter:
            results = [item for item in results if path_filter in item["path"]]
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[: max(1, min(recall, 50))]
