"""OpenGrok REST API 客户端。

通过 HTTP 调用 OpenGrok 的 REST API 和 Web 接口。

OpenGrok API 端点：
- 搜索:  GET /api/v1/search?full=query&maxresults=N&projects=a,b
- 建议:  GET /api/v1/suggest?full=query&field=full&caret=N
- 符号:  GET /api/v1/file/defs?path=/src/main.cpp
- 文件:  GET /raw/{project}/{path}
- 历史:  GET /history/{project}/{path}   (HTML)
- 项目:  GET /  → 解析 HTML <select> 或 /xref/ 链接
- 目录:  GET /xref/{project}/{path}      (HTML)
"""

from __future__ import annotations

import html
import json
import re
import asyncio
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, unquote

import httpx

from .config import OpenGrokConfig


class OpenGrokClient:
    """OpenGrok HTTP 客户端，封装搜索、文件读取、历史查询等操作。"""

    def __init__(self, config: OpenGrokConfig):
        self._base = config.base_url.rstrip("/")
        self._auth = None
        if config.username and config.password:
            self._auth = httpx.BasicAuth(config.username, config.password)
        self._verify = config.verify_ssl
        self._timeout = config.timeout_seconds
        self._default_project = config.default_project
        self._default_max = config.default_max_results
        self._retry_attempts = config.retry_attempts
        self._cache_ttl = config.cache_ttl_seconds
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._http = httpx.AsyncClient(
            verify=self._verify,
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=False,
        )
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    async def close(self) -> None:
        """释放共享 HTTP 连接池。"""
        await self._http.aclose()

    @staticmethod
    def _safe_path(path: str, *, allow_empty: bool = False) -> str:
        clean = path.strip().lstrip("/")
        if not clean and allow_empty:
            return ""
        if not clean or "\x00" in clean:
            raise ValueError("path 不能为空，且不能包含空字符")
        decoded = clean
        for _ in range(2):
            decoded = unquote(decoded)
        if any(part in {"", ".", ".."} for part in decoded.replace("\\", "/").split("/")):
            raise ValueError("path 不能包含空段、当前目录或上级目录")
        return clean

    @staticmethod
    def _path_url(path: str) -> str:
        return "/".join(quote(part, safe="") for part in path.split("/"))

    def _cache_get(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        if self._cache_ttl <= 0:
            return
        self._cache[key] = (time.monotonic() + self._cache_ttl, value)
        self._cache.move_to_end(key)
        while len(self._cache) > 256:
            self._cache.popitem(last=False)

    async def _get(self, path: str, *, params: dict[str, str] | None = None, accept: str) -> httpx.Response:
        """受并发限制的 GET；只重试网络错误、429 和 5xx。"""
        retryable_statuses = {429, 500, 502, 503, 504}
        for attempt in range(self._retry_attempts + 1):
            try:
                async with self._semaphore:
                    response = await self._http.get(
                        f"{self._base}/{path.lstrip('/')}",
                        params=params,
                        auth=self._auth,
                        headers={"Accept": accept},
                    )
                if response.status_code not in retryable_statuses or attempt == self._retry_attempts:
                    response.raise_for_status()
                    return response
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == self._retry_attempts:
                    raise
            await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        raise RuntimeError("OpenGrok 请求重试逻辑异常结束")

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        search_type: str = "full",
        projects: list[str] | None = None,
        max_results: int | None = None,
        start: int = 0,
        file_type: str | None = None,
    ) -> dict[str, Any]:
        """搜索代码。search_type: full|defs|refs|path|hist"""
        max_results = max_results or self._default_max
        params: dict[str, str] = {
            search_type: query,
            "maxresults": str(max_results),
        }
        if projects:
            params["projects"] = ",".join(projects)
        elif self._default_project:
            params["projects"] = self._default_project
        if start > 0:
            params["start"] = str(start)
        if file_type:
            params["type"] = file_type

        cache_key = f"search:{json.dumps(params, sort_keys=True)}"
        if cached := self._cache_get(cache_key):
            return cached
        r = await self._get("api/v1/search", params=params, accept="application/json")
        result = _parse_search_response(r.json(), search_type, query)
        self._cache_set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # 文件内容
    # ------------------------------------------------------------------

    async def get_file_content(
        self,
        project: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        """读取文件内容，支持行范围。"""
        clean = self._safe_path(path)
        cache_key = f"file:{project}:{clean}"
        full = self._cache_get(cache_key)
        if full is None:
            r = await self._get(
                f"raw/{quote(project, safe='')}/{self._path_url(clean)}",
                accept="text/plain, */*",
            )
            full = r.text
            self._cache_set(cache_key, full)
        lines = full.split("\n")
        total = len(lines) if full else 0
        if start_line is not None or end_line is not None:
            s = max(0, (start_line or 1) - 1)
            e = end_line if end_line is not None else len(lines)
            content = "\n".join(lines[s:e])
        else:
            content = full
        return {
            "project": project,
            "path": clean,
            "content": content,
            "lineCount": total,
            "sizeBytes": len(full.encode("utf-8")),
            "startLine": start_line,
        }

    # ------------------------------------------------------------------
    # 文件历史
    # ------------------------------------------------------------------

    async def get_file_history(
        self,
        project: str,
        path: str,
        max_entries: int = 10,
    ) -> dict[str, Any]:
        """获取文件提交历史（解析 HTML 页面）。"""
        clean = self._safe_path(path)
        cache_key = f"history:{project}:{clean}"
        entries = self._cache_get(cache_key)
        if entries is None:
            r = await self._get(
                f"history/{quote(project, safe='')}/{self._path_url(clean)}",
                accept="text/html, */*",
            )
            entries = _parse_history_html(r.text, 50)
            self._cache_set(cache_key, entries)
        return {"project": project, "path": clean, "entries": entries[:max_entries]}

    # ------------------------------------------------------------------
    # 项目列表
    # ------------------------------------------------------------------

    async def list_projects(self) -> list[dict[str, str]]:
        """列出所有已索引的项目（解析首页 HTML）。"""
        cached = self._cache_get("projects")
        if cached is not None:
            return cached
        r = await self._get("", accept="text/html, */*")
        projects = _parse_projects_html(r.text)
        self._cache_set("projects", projects)
        return projects

    # ------------------------------------------------------------------
    # 浏览目录
    # ------------------------------------------------------------------

    async def browse_directory(self, project: str, path: str = "") -> list[dict[str, Any]]:
        """浏览项目目录结构（解析 xref HTML 页面）。"""
        clean = self._safe_path(path, allow_empty=True)
        url_path = f"{clean}/" if clean else ""
        r = await self._get(
            f"xref/{quote(project, safe='')}/{self._path_url(url_path)}",
            accept="text/html, */*",
        )
        return _parse_directory_html(r.text, project, clean)

    # ------------------------------------------------------------------
    # 搜索建议
    # ------------------------------------------------------------------

    async def suggest(
        self,
        query: str,
        project: str | None = None,
        field: str = "full",
    ) -> list[str]:
        """获取搜索建议。"""
        params = {
            field: query,
            "field": field,
            "caret": str(len(query)),
        }
        if project:
            params["projects"] = project
        r = await self._get("api/v1/suggest", params=params, accept="application/json")
        data = r.json()
        return data.get("suggestions", [])

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    async def test_connection(self) -> bool:
        """测试 OpenGrok 服务器连通性。"""
        try:
            r = await self._http.get(self._base, auth=self._auth)
            return r.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 逐行 Blame (annotate)
    # ------------------------------------------------------------------

    async def get_annotate(
        self,
        project: str,
        path: str,
    ) -> dict[str, Any]:
        """获取文件逐行 blame 信息（解析 annotate/xref HTML 页面）。"""
        clean = self._safe_path(path)
        cache_key = f"annotate:{project}:{clean}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            response = await self._get(
                f"annotate/{quote(project, safe='')}/{self._path_url(clean)}",
                accept="text/html, */*",
            )
            lines = _parse_annotate_html(response.text)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise
            lines = []
        # 部分旧版会以 200 返回不含 annotate 数据的页面，同样要回退到 xref。
        if not lines:
            response = await self._get(
                f"xref/{quote(project, safe='')}/{self._path_url(clean)}",
                params={"a": "true"},
                accept="text/html, */*",
            )
            lines = _parse_annotate_html(response.text)
        result = {"project": project, "path": clean, "lines": lines}
        self._cache_set(cache_key, result)
        return result

    # ------------------------------------------------------------------
    # 符号与组合查询
    # ------------------------------------------------------------------

    async def get_file_symbols(self, project: str, path: str) -> dict[str, Any]:
        """读取文件内已被 OpenGrok 索引的符号定义。"""
        clean = self._safe_path(path)
        cache_key = f"symbols:{project}:{clean}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        r = await self._get(
            "api/v1/file/defs",
            params={"path": f"/{clean}", "projects": project},
            accept="application/json",
        )
        raw = r.json()
        if isinstance(raw, dict):
            raw = raw.get("symbols", raw.get("results", []))
        symbols = raw if isinstance(raw, list) else []
        result = {"project": project, "path": clean, "symbols": symbols}
        self._cache_set(cache_key, result)
        return result

    async def get_symbol_context(
        self,
        symbol: str,
        project: str | None = None,
        context_lines: int = 30,
    ) -> dict[str, Any]:
        """合并定义、声明附近源码与引用，减少 Agent 往返。"""
        if not symbol.strip():
            raise ValueError("symbol 不能为空")
        projects = [project] if project else None
        definitions, references = await asyncio.gather(
            self.search(symbol, "defs", projects, 10),
            self.search(symbol, "refs", projects, 20),
        )
        contexts: list[dict[str, Any]] = []
        for definition in definitions["results"][:3]:
            matches = definition.get("matches", [])
            line = matches[0].get("lineNumber", 1) if matches else 1
            try:
                source = await self.get_file_content(
                    definition["project"],
                    definition["path"],
                    max(1, line - context_lines),
                    line + context_lines,
                )
                contexts.append(source)
            except httpx.HTTPError as exc:
                contexts.append({
                    "project": definition["project"], "path": definition["path"],
                    "error": str(exc),
                })
        return {
            "symbol": symbol,
            "definitions": definitions,
            "definitionContexts": contexts,
            "references": references,
        }

    async def search_and_read(
        self,
        query: str,
        search_type: str = "full",
        projects: list[str] | None = None,
        max_results: int = 10,
        context_lines: int = 20,
        file_type: str | None = None,
    ) -> dict[str, Any]:
        """搜索并并发获取排名靠前结果的局部源码。"""
        search_result = await self.search(
            query, search_type, projects, max_results, file_type=file_type,
        )

        async def read_result(result: dict[str, Any]) -> dict[str, Any]:
            matches = result.get("matches", [])
            line = matches[0].get("lineNumber", 1) if matches else 1
            try:
                return await self.get_file_content(
                    result["project"], result["path"],
                    max(1, line - context_lines), line + context_lines,
                )
            except httpx.HTTPError as exc:
                return {"project": result["project"], "path": result["path"], "error": str(exc)}

        files = await asyncio.gather(*(read_result(result) for result in search_result["results"][:5]))
        return {"search": search_result, "files": files}

    # ------------------------------------------------------------------
    # 近期变更 (what_changed)
    # ------------------------------------------------------------------

    async def what_changed(
        self,
        project: str,
        path: str,
        since_days: int = 14,
    ) -> dict[str, Any]:
        """获取近期变更：历史 + blame 按提交分组。

        原理：先获取文件历史找到近期 revision，再获取 annotate 找到哪些行属于这些 revision。
        """
        from datetime import datetime, timedelta, timezone

        history_data, annotate_data = await asyncio.gather(
            self.get_file_history(project, path, 50),
            self.get_annotate(project, path),
        )

        # 确定截止日期
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

        # 找到近期 revision
        recent_revisions: set[str] = set()
        for entry in history_data["entries"]:
            try:
                # OpenGrok 日期格式: "2026-01-01 12:00" 或 "2026-01-01"
                date_str = entry["date"].strip()
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        d = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                        if d >= cutoff:
                            recent_revisions.add(entry["revision"])
                        break
                    except ValueError:
                        continue
            except Exception:
                continue

        # 按 revision 分组 annotate 行
        by_revision: dict[str, dict] = {}
        for line in annotate_data["lines"]:
            rev = line["revision"]
            if not rev or rev not in recent_revisions:
                continue
            if rev not in by_revision:
                by_revision[rev] = {
                    "revision": rev,
                    "author": line["author"],
                    "date": line["date"],
                    "lines": [],
                }
            by_revision[rev]["lines"].append({
                "lineNumber": line["lineNumber"],
                "content": line["content"],
            })

        # 按日期排序
        changes = sorted(by_revision.values(), key=lambda x: x["date"], reverse=True)

        return {
            "project": project,
            "path": path,
            "sinceDays": since_days,
            "recentRevisions": len(recent_revisions),
            "changes": changes,
        }


# ======================================================================
# 内部解析函数
# ======================================================================

def _parse_search_response(
    data: dict[str, Any],
    search_type: str,
    query: str,
) -> dict[str, Any]:
    """解析 OpenGrok REST API 搜索响应。"""
    result_count = data.get("resultCount", 0)
    raw_results = data.get("results", {}) or {}
    # 结果按项目分组: { "project_name": [ {line, lineNumber, path}, ... ] }
    results: list[dict[str, Any]] = []
    for project_name, entries in raw_results.items():
        for entry in entries:
            if isinstance(entry, str):
                # 某些旧版本返回字符串列表
                results.append({
                    "project": project_name,
                    "path": entry,
                    "matches": [],
                })
            else:
                results.append({
                    "project": project_name,
                    "path": entry.get("path", ""),
                    "matches": [
                        {
                            "lineNumber": entry.get("lineNumber", 0),
                            "lineContent": entry.get("line", ""),
                        }
                    ],
                })
    return {
        "query": query,
        "searchType": search_type,
        "totalCount": result_count,
        "startIndex": data.get("start", 0),
        "endIndex": data.get("end", 0),
        "results": results,
    }


def _parse_projects_html(html_text: str) -> list[dict[str, str]]:
    """从 OpenGrok 首页 HTML 解析项目列表。"""
    projects: list[dict[str, str]] = []
    # 方法1: 解析 <select id="project"> 或 <select name="project">
    select_match = re.search(
        r'<select[^>]*(?:id|name)\s*=\s*["\']project["\'][^>]*>(.*?)</select>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    if select_match:
        body = select_match.group(1)
        current_group = ""
        for optgroup_match in re.finditer(
            r'<optgroup[^>]*label\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</optgroup>',
            body, re.DOTALL | re.IGNORECASE,
        ):
            current_group = optgroup_match.group(1)
            for opt in re.finditer(
                r'<option[^>]*value\s*=\s*["\']([^"\']*)["\'][^>]*>',
                optgroup_match.group(2), re.IGNORECASE,
            ):
                v = opt.group(1).strip()
                if v:
                    projects.append({"name": v, "category": current_group})
        # 直接的 <option> (不在 optgroup 中)
        for opt in re.finditer(
            r'<option[^>]*value\s*=\s*["\']([^"\']*)["\'][^>]*>',
            body, re.IGNORECASE,
        ):
            v = opt.group(1).strip()
            if v and not any(p["name"] == v for p in projects):
                projects.append({"name": v})
        return projects

    # 方法2: 解析 /xref/ 链接（fallback）
    seen = set()
    for m in re.finditer(r'href="[^"]*/xref/([^/"]+)', html_text, re.IGNORECASE):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            projects.append({"name": name})
    return projects


def _parse_history_html(html_text: str, max_entries: int) -> list[dict[str, Any]]:
    """从 OpenGrok 文件历史 HTML 页面解析提交记录。"""
    entries: list[dict[str, Any]] = []
    # 匹配 <tr class="..."> 中的历史行
    rows = re.findall(
        r'<tr[^>]*>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?'
        r'<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    for rev_cell, date_cell, author_cell, msg_cell in rows[:max_entries]:
        rev = _strip_tags(rev_cell).strip()
        date = _strip_tags(date_cell).strip()
        author = _strip_tags(author_cell).strip()
        message = _strip_tags(msg_cell).strip()
        if rev and date:
            entries.append({
                "revision": rev,
                "date": date,
                "author": author,
                "message": html.unescape(message),
            })
    return entries


def _parse_directory_html(
    html_text: str,
    project: str,
    current_path: str,
) -> list[dict[str, Any]]:
    """从 OpenGrok xref 目录页面解析文件列表。"""
    entries: list[dict[str, Any]] = []
    prefix = "" if not current_path else current_path + "/"
    # 匹配 href="/xref/PROJECT/PATH" 或 href="/xref/PROJECT/PATH"（末尾可能有 /）
    escaped = re.escape(project)
    for m in re.finditer(
        rf'href="(?:https?://[^/]+)?/xref/{escaped}/(.+?)"',
        html_text, re.IGNORECASE,
    ):
        entry_path = m.group(1).rstrip("/")
        if entry_path == current_path or entry_path == prefix.rstrip("/"):
            continue
        # 确定名称：去掉前缀，取最后一段
        name = entry_path[len(prefix):] if entry_path.startswith(prefix) else entry_path
        # 判断是否为目录：href 原值末尾有 / 的为目录
        raw = m.group(1)
        is_dir = raw.endswith("/")
        entries.append({
            "name": name,
            "isDirectory": is_dir,
            "path": entry_path,
        })
    return entries


def _strip_tags(text: str) -> str:
    """去除 HTML 标签。"""
    return re.sub(r"<[^>]*>", "", text)


def _parse_annotate_html(html_text: str) -> list[dict[str, Any]]:
    """从 OpenGrok annotate/xref 页面解析逐行 blame 信息。

    支持两种格式：
    1. span.blame 格式（OpenGrok 1.12+）：每个 <span class="blame"> 一行
    2. table 格式（回退）：<tr> 中包括 revision、author、内容
    """
    lines: list[dict[str, Any]] = []

    # 方法1: 解析 <span class="blame"> 格式
    # title 格式: "revision: abc123, author: dev, date: 2026-01-01 12:00"
    blame_spans = re.findall(
        r'<span[^>]*class="blame"[^>]*title="([^"]*)"[^>]*>(.*?)</span>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    if blame_spans:
        line_num = 0
        for title, content in blame_spans:
            line_num += 1
            # 提取 revision
            rev = ""
            m = re.search(r'(?:revision|changeset):\s*([a-f0-9]+)', title, re.IGNORECASE)
            if m:
                rev = m.group(1)
            # 提取 author — 取逗号前的内容，格式: "author: dev, date: ..."
            author = ""
            m = re.search(r'(?:author|user):\s*([^,]+)', title, re.IGNORECASE)
            if m:
                author = m.group(1).strip()
            # 提取 date
            date = ""
            m = re.search(r'date:\s*(.+?)(?:<br|$)', title, re.IGNORECASE)
            if m:
                date = m.group(1).strip()
            # 清理内容
            content = _strip_tags(content).strip()
            lines.append({
                "lineNumber": line_num,
                "revision": rev,
                "author": author,
                "date": date,
                "content": html.unescape(content),
            })
        return lines

    # 方法2: table 格式回退
    table_rows = re.findall(
        r'<tr[^>]*>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>',
        html_text, re.DOTALL | re.IGNORECASE,
    )
    for idx, (rev_cell, author_cell, content_cell) in enumerate(table_rows, 1):
        lines.append({
            "lineNumber": idx,
            "revision": _strip_tags(rev_cell).strip(),
            "author": _strip_tags(author_cell).strip(),
            "date": "",
            "content": _strip_tags(content_cell).strip(),
        })

    return lines
