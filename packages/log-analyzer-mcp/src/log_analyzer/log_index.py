"""面向超大文本日志的持久化分块索引。

索引以行边界切分日志，并保留相邻上下文。支持 FTS5 trigram 时，SQLite 可用
LIKE 对字面量子串筛选候选块；较旧 SQLite 自动退化为块扫描，但仍避免把完整
日志载入内存。最终匹配始终由 Python 按调用者要求的大小写语义复核。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Iterable

from .domain import Artifact


INDEX_VERSION = 1
INDEX_CONTEXT_LINES = 64
INDEX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_LOGICAL_LINE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class IndexLimits:
    max_input_bytes: int = 16 * 1024 * 1024 * 1024
    max_storage_bytes: int = 16 * 1024 * 1024 * 1024
    chunk_bytes: int = 1024 * 1024
    max_build_seconds: float = 600.0
    max_search_seconds: float = 30.0


@dataclass(frozen=True)
class IndexBuildResult:
    artifact_count: int
    chunk_count: int
    indexed_bytes: int
    reused: bool
    truncated: bool
    warnings: list[str]


@dataclass(frozen=True)
class IndexMatch:
    artifact_id: str
    relative_path: str
    line_number: int
    line: str
    before: list[str]
    after: list[str]


@dataclass(frozen=True)
class IndexSearchResult:
    matches: list[IndexMatch]
    candidate_chunks: int
    indexed_artifact_ids: set[str]
    truncated: bool = False


class IndexRejected(Exception):
    pass


def _fingerprint(items: Iterable[tuple[Artifact, Path]]) -> str:
    records = []
    for artifact, path in items:
        stat_result = path.stat()
        records.append((artifact.artifact_id, stat_result.st_size, stat_result.st_mtime_ns))
    payload = json.dumps(sorted(records), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _detect_encoding(path: Path) -> str:
    with path.open("rb") as handle:
        sample = handle.read(64 * 1024)
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if sample:
        even_zero = sample[0::2].count(0) / max(len(sample[0::2]), 1)
        odd_zero = sample[1::2].count(0) / max(len(sample[1::2]), 1)
        if odd_zero > 0.3 and even_zero < 0.05:
            return "utf-16-le"
        if even_zero > 0.3 and odd_zero < 0.05:
            return "utf-16-be"
        if b"\x00" in sample:
            raise IndexRejected("样本包含 NUL 且不像 UTF-16 文本")
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        try:
            sample.decode("gb18030")
            return "gb18030"
        except UnicodeDecodeError:
            return "utf-8"


def _safe_lines(path: Path):
    encoding = _detect_encoding(path)
    with path.open("r", encoding=encoding, errors="replace") as handle:
        line_number = 0
        while True:
            raw = handle.readline(MAX_LOGICAL_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw.encode("utf-8", errors="replace")) > MAX_LOGICAL_LINE_BYTES and not raw.endswith(("\n", "\r")):
                raise IndexRejected(f"存在超过 {MAX_LOGICAL_LINE_BYTES} 字节的单行")
            line_number += 1
            yield line_number, raw.rstrip("\r\n")


def _iter_chunks(path: Path, chunk_bytes: int):
    iterator = iter(_safe_lines(path))
    prefix: list[tuple[int, str]] = []
    carry: list[tuple[int, str]] = []
    exhausted = False

    while carry or not exhausted:
        primary = carry
        carry = []
        primary_bytes = sum(len(line.encode("utf-8", errors="replace")) + 1 for _, line in primary)
        while primary_bytes < chunk_bytes and not exhausted:
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                break
            primary.append(item)
            primary_bytes += len(item[1].encode("utf-8", errors="replace")) + 1
        if not primary:
            break

        carry_bytes = 0
        for _ in range(INDEX_CONTEXT_LINES):
            try:
                item = next(iterator)
            except StopIteration:
                exhausted = True
                break
            carry.append(item)
            carry_bytes += len(item[1].encode("utf-8", errors="replace")) + 1
            if carry_bytes >= INDEX_CONTEXT_BYTES:
                break

        context = prefix + primary + carry
        yield {
            "content_start_line": context[0][0],
            "primary_start_line": primary[0][0],
            "primary_end_line": primary[-1][0],
            "content": "\n".join(line for _, line in context),
        }
        prefix = []
        prefix_bytes = 0
        for item in reversed(primary[-INDEX_CONTEXT_LINES:]):
            item_bytes = len(item[1].encode("utf-8", errors="replace")) + 1
            if prefix and prefix_bytes + item_bytes > INDEX_CONTEXT_BYTES:
                break
            prefix.append(item)
            prefix_bytes += item_bytes
        prefix.reverse()


class LogIndex:
    def __init__(self, path: Path, limits: IndexLimits):
        self.path = path
        self.limits = limits

    def build(self, artifacts: list[tuple[Artifact, Path]], *, force: bool = False) -> IndexBuildResult:
        candidates = [
            (artifact, path) for artifact, path in artifacts
            if artifact.readable_text and path.is_file() and not path.is_symlink()
        ]
        fingerprint = _fingerprint(candidates)
        if not force:
            cached = self._cached_result(fingerprint)
            if cached is not None:
                return cached

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        warnings: list[str] = []
        indexed_bytes = 0
        chunk_count = 0
        artifact_count = 0
        truncated = False
        deadline = time.monotonic() + self.limits.max_build_seconds
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "CREATE TABLE indexed_artifacts (artifact_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, size_bytes INTEGER NOT NULL)"
            )
            fts_mode = self._create_chunks_table(connection)

            for artifact, path in candidates:
                if time.monotonic() > deadline:
                    warnings.append("索引构建达到运行时间预算")
                    truncated = True
                    break
                file_size = path.stat().st_size
                if indexed_bytes + file_size > self.limits.max_input_bytes:
                    warnings.append(f"{artifact.relative_path} 未索引：达到索引输入字节预算")
                    truncated = True
                    continue
                connection.execute("SAVEPOINT artifact_build")
                local_chunks = 0
                try:
                    for chunk in _iter_chunks(path, self.limits.chunk_bytes):
                        if time.monotonic() > deadline:
                            raise IndexRejected("索引构建达到运行时间预算")
                        connection.execute(
                            "INSERT INTO chunks (artifact_id, relative_path, content_start_line, primary_start_line, primary_end_line, content) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                artifact.artifact_id,
                                artifact.relative_path,
                                chunk["content_start_line"],
                                chunk["primary_start_line"],
                                chunk["primary_end_line"],
                                chunk["content"],
                            ),
                        )
                        local_chunks += 1
                    page_count = connection.execute("PRAGMA page_count").fetchone()[0]
                    page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                    if page_count * page_size > self.limits.max_storage_bytes:
                        raise IndexRejected("索引文件达到存储预算")
                    connection.execute(
                        "INSERT INTO indexed_artifacts VALUES (?, ?, ?)",
                        (artifact.artifact_id, artifact.relative_path, file_size),
                    )
                    connection.execute("RELEASE artifact_build")
                except (OSError, UnicodeError, IndexRejected) as exc:
                    connection.execute("ROLLBACK TO artifact_build")
                    connection.execute("RELEASE artifact_build")
                    warnings.append(f"{artifact.relative_path} 未索引：{exc}")
                    truncated = True
                    continue
                indexed_bytes += file_size
                chunk_count += local_chunks
                artifact_count += 1

            metadata = {
                "version": INDEX_VERSION,
                "fingerprint": fingerprint,
                "fts_mode": fts_mode,
                "artifact_count": artifact_count,
                "chunk_count": chunk_count,
                "indexed_bytes": indexed_bytes,
                "truncated": truncated,
                "warnings": warnings,
            }
            connection.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()],
            )
            connection.commit()
        except Exception:
            connection.close()
            if temporary.exists():
                temporary.unlink()
            raise
        finally:
            connection.close()

        if self.path.exists():
            self.path.unlink()
        temporary.replace(self.path)
        return IndexBuildResult(artifact_count, chunk_count, indexed_bytes, False, truncated, warnings)

    def _create_chunks_table(self, connection: sqlite3.Connection) -> str:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE chunks USING fts5(artifact_id UNINDEXED, relative_path UNINDEXED, content_start_line UNINDEXED, primary_start_line UNINDEXED, primary_end_line UNINDEXED, content, tokenize='trigram')"
            )
            return "fts5-trigram"
        except sqlite3.OperationalError:
            connection.execute(
                "CREATE TABLE chunks (artifact_id TEXT NOT NULL, relative_path TEXT NOT NULL, content_start_line INTEGER NOT NULL, primary_start_line INTEGER NOT NULL, primary_end_line INTEGER NOT NULL, content TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX chunks_artifact ON chunks(artifact_id, primary_start_line)")
            return "chunk-scan"

    def _read_meta(self, connection: sqlite3.Connection) -> dict | None:
        try:
            rows = connection.execute("SELECT key, value FROM meta").fetchall()
            return {key: json.loads(value) for key, value in rows}
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def _cached_result(self, fingerprint: str) -> IndexBuildResult | None:
        if not self.path.is_file():
            return None
        try:
            connection = sqlite3.connect(self.path)
            try:
                meta = self._read_meta(connection)
            finally:
                connection.close()
            if not meta or meta.get("version") != INDEX_VERSION or meta.get("fingerprint") != fingerprint:
                return None
            return IndexBuildResult(
                int(meta["artifact_count"]),
                int(meta["chunk_count"]),
                int(meta["indexed_bytes"]),
                True,
                bool(meta["truncated"]),
                list(meta["warnings"]),
            )
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
            return None

    def indexed_artifact_ids(self) -> set[str]:
        if not self.path.is_file():
            return set()
        try:
            connection = sqlite3.connect(self.path)
            try:
                return {row[0] for row in connection.execute("SELECT artifact_id FROM indexed_artifacts")}
            finally:
                connection.close()
        except sqlite3.Error:
            return set()

    def search(
        self,
        query: str,
        *,
        artifact_ids: list[str] | None,
        case_sensitive: bool,
        context_before: int,
        context_after: int,
        max_results: int,
    ) -> IndexSearchResult:
        indexed_ids = self.indexed_artifact_ids()
        selected_ids = indexed_ids & set(artifact_ids or indexed_ids)
        if not selected_ids or not self.path.is_file():
            return IndexSearchResult([], 0, indexed_ids)

        where = []
        arguments: list[object] = []
        placeholders = ",".join("?" for _ in selected_ids)
        where.append(f"artifact_id IN ({placeholders})")
        arguments.extend(sorted(selected_ids))
        # Trigram-enabled LIKE can prune ASCII substring candidates. For short or
        # non-ASCII queries, scan indexed chunks and preserve Python casefold semantics.
        if len(query) >= 3 and query.isascii():
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("content LIKE ? ESCAPE '\\'")
            arguments.append(f"%{escaped}%")
        sql = (
            "SELECT artifact_id, relative_path, content_start_line, primary_start_line, primary_end_line, content "
            f"FROM chunks WHERE {' AND '.join(where)} ORDER BY artifact_id, CAST(primary_start_line AS INTEGER)"
        )

        matches: list[IndexMatch] = []
        seen: set[tuple[str, int]] = set()
        candidate_chunks = 0
        connection = sqlite3.connect(self.path)
        deadline = time.monotonic() + self.limits.max_search_seconds
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            try:
                for row in connection.execute(sql, arguments):
                    if time.monotonic() > deadline:
                        return IndexSearchResult(matches, candidate_chunks, indexed_ids, truncated=True)
                    candidate_chunks += 1
                    artifact_id, relative_path = str(row[0]), str(row[1])
                    content_start = int(row[2])
                    primary_start, primary_end = int(row[3]), int(row[4])
                    lines = str(row[5]).split("\n")
                    for index, line in enumerate(lines):
                        line_number = content_start + index
                        if line_number < primary_start or line_number > primary_end:
                            continue
                        haystack = line if case_sensitive else line.casefold()
                        needle = query if case_sensitive else query.casefold()
                        key = (artifact_id, line_number)
                        if needle not in haystack or key in seen:
                            continue
                        seen.add(key)
                        matches.append(IndexMatch(
                            artifact_id=artifact_id,
                            relative_path=relative_path,
                            line_number=line_number,
                            line=line,
                            before=lines[max(0, index - context_before):index],
                            after=lines[index + 1:index + 1 + context_after],
                        ))
                        if len(matches) >= max_results:
                            return IndexSearchResult(matches, candidate_chunks, indexed_ids)
            except sqlite3.OperationalError as exc:
                if "interrupted" not in str(exc).casefold():
                    raise
                return IndexSearchResult(matches, candidate_chunks, indexed_ids, truncated=True)
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
        return IndexSearchResult(matches, candidate_chunks, indexed_ids)
