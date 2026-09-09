"""OpenGrok REST API 客户端。"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .config import OpenGrokConfig


class OpenGrokProjectDiscoveryError(RuntimeError):
    """Raised when an unscoped symbol search cannot obtain index projects."""


class OpenGrokClient:
    """OpenGrok HTTP 客户端。"""

    def __init__(self, config: OpenGrokConfig):
        self._base = config.base_url.rstrip("/")
        self._auth = None
        if config.username and config.password:
            self._auth = httpx.BasicAuth(config.username, config.password)
        self._verify = config.verify_ssl
        self._timeout = config.timeout_seconds
        self._default_project = config.default_project
        self._default_max = config.default_max_results

    async def search(
        self, query: str, search_type: str = "full",
        projects: list[str] | None = None, max_results: int | None = None,
        start: int = 0, file_type: str | None = None,
    ) -> dict[str, Any]:
        """搜索代码；对旧版 OpenGrok 的 defs/refs 400 自动兜底。"""
        max_results = max_results or self._default_max
        effective_projects = projects or ([self._default_project] if self._default_project else None)
        try:
            return await self._search_api(
                query, search_type, effective_projects, max_results, start, file_type
            )
        except httpx.HTTPStatusError as first_error:
            # Old OpenGrok instances commonly reject unscoped defs/refs REST
            # searches. Discover the indexed projects instead of exposing a
            # bare 400 to the investigation agent.
            if first_error.response.status_code != 400 or search_type not in {"defs", "refs"}:
                raise

            if not effective_projects:
                discovered = await self.list_projects()
                effective_projects = [item["name"] for item in discovered if item.get("name")]
                if not effective_projects:
                    raise OpenGrokProjectDiscoveryError(
                        f"{search_type} 搜索无法发现任何已索引项目；请检查 OpenGrok 项目索引。"
                    ) from first_error
                try:
                    return await self._search_api(
                        query, search_type, effective_projects, max_results, start, file_type
                    )
                except httpx.HTTPStatusError as scoped_error:
                    if scoped_error.response.status_code != 400:
                        raise

            # Some older REST APIs do not implement defs/refs at all. A scoped
            # full-text search is less precise, but preserves source discovery
            # and makes the fallback explicit to the caller.
            result = await self._search_api(
                query, "full", effective_projects, max_results, start, file_type
            )
            result["fallbackFrom"] = search_type
            result["note"] = (
                f"OpenGrok REST 不支持 {search_type} 搜索；已使用限定项目范围的 full 搜索兜底。"
            )
            return result

    async def _search_api(
        self, query: str, search_type: str, projects: list[str] | None,
        max_results: int, start: int, file_type: str | None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {search_type: query, "maxresults": str(max_results)}
        if projects:
            params["projects"] = ",".join(projects)
        if start > 0:
            params["start"] = str(start)
        if file_type:
            params["type"] = file_type
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            response = await c.get(
                f"{self._base}/api/v1/search",
                params=params,
                auth=self._auth,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return _parse_search(response.json(), search_type, query)

    async def get_file_content(
        self, project: str, path: str,
        start_line: int | None = None, end_line: int | None = None,
    ) -> dict[str, Any]:
        """读取文件内容，支持行范围。"""
        clean = path.lstrip("/")
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            r = await c.get(f"{self._base}/raw/{quote(project)}/{clean}", auth=self._auth, headers={"Accept": "text/plain"})
            r.raise_for_status()
            full = r.text
            lines = full.split("\n")
            total = len(lines) if full else 0
            s = max(0, (start_line or 1) - 1)
            e = end_line if end_line is not None else len(lines)
            return {"project": project, "path": clean, "content": "\n".join(lines[s:e]), "lineCount": total, "sizeBytes": len(full.encode("utf-8")), "startLine": start_line}

    async def get_file_history(self, project: str, path: str, max_entries: int = 10) -> dict[str, Any]:
        """获取文件提交历史。如果 OpenGrok 未集成 SCM，返回空列表。"""
        clean = path.lstrip("/")
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            r = await c.get(f"{self._base}/history/{quote(project)}/{clean}", auth=self._auth, headers={"Accept": "text/html"})
            if r.status_code == 404:
                return {"project": project, "path": clean, "entries": [], "note": "History not available (SCM may not be integrated)"}
            r.raise_for_status()
            return {"project": project, "path": clean, "entries": _parse_history(r.text, max_entries)}

    async def list_projects(self) -> list[dict[str, str]]:
        """列出所有已索引的项目。"""
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            r = await c.get(self._base, auth=self._auth, headers={"Accept": "text/html"})
            r.raise_for_status()
            return _parse_projects(r.text)

    async def browse_directory(self, project: str, path: str = "") -> list[dict[str, Any]]:
        """浏览项目目录结构。"""
        clean = path.strip("/")
        url_path = f"{clean}/" if clean else ""
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            r = await c.get(f"{self._base}/xref/{quote(project)}/{url_path}", auth=self._auth, headers={"Accept": "text/html"})
            r.raise_for_status()
            return _parse_dir(r.text, project, clean)

    async def suggest(self, query: str, project: str | None = None, field: str = "full") -> list[str]:
        """获取搜索建议。"""
        params = {field: query, "field": field, "caret": str(len(query))}
        if project:
            params["projects"] = project
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            r = await c.get(f"{self._base}/api/v1/suggest", params=params, auth=self._auth, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json().get("suggestions", [])

    async def test_connection(self) -> bool:
        """测试连通性。"""
        try:
            async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
                r = await c.get(self._base, auth=self._auth)
                return r.status_code < 500
        except Exception:
            return False

    async def get_annotate(self, project: str, path: str) -> dict[str, Any]:
        """逐行 blame。如果 SCM 未集成，返回空列表。"""
        clean = path.lstrip("/")
        async with httpx.AsyncClient(verify=self._verify, timeout=self._timeout, follow_redirects=True) as c:
            try:
                r = await c.get(f"{self._base}/annotate/{quote(project)}/{clean}", auth=self._auth, headers={"Accept": "text/html"})
                if r.status_code == 200:
                    lines = _parse_annotate(r.text)
                    if lines:
                        return {"project": project, "path": clean, "lines": lines}
                r = await c.get(f"{self._base}/xref/{quote(project)}/{clean}", params={"a": "true"}, auth=self._auth, headers={"Accept": "text/html"})
                if r.status_code == 200:
                    lines = _parse_annotate(r.text)
                    return {"project": project, "path": clean, "lines": lines}
            except Exception:
                pass
            return {"project": project, "path": clean, "lines": [], "note": "Annotate not available (SCM may not be integrated)"}

    async def what_changed(self, project: str, path: str, since_days: int = 14) -> dict[str, Any]:
        """近期变更：历史 + blame 按提交分组。SCM 未集成时返回空列表。"""
        history_data = await self.get_file_history(project, path, 50)
        annotate_data = await self.get_annotate(project, path)
        if not annotate_data.get("lines"):
            return {"project": project, "path": path, "sinceDays": since_days, "recentRevisions": 0, "changes": [], "note": "SCM not integrated"}
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        recent: set[str] = set()
        for entry in history_data["entries"]:
            try:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        if datetime.strptime(entry["date"].strip(), fmt).replace(tzinfo=timezone.utc) >= cutoff:
                            recent.add(entry["revision"])
                        break
                    except ValueError:
                        continue
            except Exception:
                continue
        by_rev: dict[str, dict] = {}
        for line in annotate_data["lines"]:
            rev = line["revision"]
            if not rev or rev not in recent:
                continue
            if rev not in by_rev:
                by_rev[rev] = {"revision": rev, "author": line["author"], "date": line["date"], "lines": []}
            by_rev[rev]["lines"].append({"lineNumber": line["lineNumber"], "content": line["content"]})
        changes = sorted(by_rev.values(), key=lambda x: x["date"], reverse=True)
        return {"project": project, "path": path, "sinceDays": since_days, "recentRevisions": len(recent), "changes": changes}


# ======================================================================
# 解析函数
# ======================================================================

def _parse_search(data: dict, stype: str, query: str) -> dict:
    result_count = data.get("resultCount", 0)
    raw = data.get("results", {}) or {}
    results: list[dict] = []
    for key, entries in raw.items():
        p = key.lstrip("/")
        parts = p.split("/", 1)
        proj = parts[0] if len(parts) > 1 else ""
        fpath = parts[1] if len(parts) > 1 else p
        if not isinstance(entries, list):
            continue
        for e in entries:
            if isinstance(e, str):
                results.append({"project": proj, "path": fpath, "matches": []})
            else:
                ln = e.get("lineNumber", 0)
                results.append({"project": proj, "path": fpath, "matches": [{"lineNumber": int(ln) if ln else 0, "lineContent": e.get("line", "")}]})
    return {"query": query, "searchType": stype, "totalCount": result_count, "startIndex": data.get("start", 0), "endIndex": data.get("end", 0), "results": results}


def _parse_projects(html_text: str) -> list[dict]:
    projects: list[dict] = []
    m = re.search(r'<select[^>]*(?:id|name)\s*=\s*["\']project["\'][^>]*>(.*?)</select>', html_text, re.DOTALL | re.IGNORECASE)
    if m:
        body = m.group(1)
        grp = ""
        for og in re.finditer(r'<optgroup[^>]*label\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</optgroup>', body, re.DOTALL | re.IGNORECASE):
            grp = og.group(1)
            for opt in re.finditer(r'<option[^>]*value\s*=\s*["\']([^"\']*)["\'][^>]*>', og.group(2), re.IGNORECASE):
                v = opt.group(1).strip()
                if v:
                    projects.append({"name": v, "category": grp})
        for opt in re.finditer(r'<option[^>]*value\s*=\s*["\']([^"\']*)["\'][^>]*>', body, re.IGNORECASE):
            v = opt.group(1).strip()
            if v and not any(p["name"] == v for p in projects):
                projects.append({"name": v})
        return projects
    seen = set()
    for m in re.finditer(r'href="[^"]*/xref/([^/"]+)', html_text, re.IGNORECASE):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            projects.append({"name": name})
    return projects


def _parse_history(html_text: str, max_entries: int) -> list[dict]:
    entries: list[dict] = []
    rows = re.findall(r'<tr[^>]*>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>', html_text, re.DOTALL | re.IGNORECASE)
    for rev_cell, date_cell, author_cell, msg_cell in rows[:max_entries]:
        rev = _strip(rev_cell).strip()
        date = _strip(date_cell).strip()
        author = _strip(author_cell).strip()
        msg = _strip(msg_cell).strip()
        if rev and date:
            entries.append({"revision": rev, "date": date, "author": author, "message": html.unescape(msg)})
    return entries


def _parse_dir(html_text: str, project: str, current_path: str) -> list[dict]:
    entries: list[dict] = []
    prefix = "" if not current_path else current_path + "/"
    escaped = re.escape(project)
    seen = set()
    # 只匹配 /source/download/ 链接（实际文件），不匹配 /source/xref/ 面包屑
    for m in re.finditer(rf'href="/source/download/{escaped}/(.+?)"', html_text, re.IGNORECASE):
        ep = m.group(1).rstrip("/")
        if not ep or ep == current_path or ep == prefix.rstrip("/"):
            continue
        if ep in seen:
            continue
        seen.add(ep)
        name = ep[len(prefix):] if ep.startswith(prefix) else ep.rsplit("/", 1)[-1]
        entries.append({"name": name, "isDirectory": False, "path": ep})
    return entries


def _strip(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text)


def _parse_annotate(html_text: str) -> list[dict]:
    lines: list[dict] = []
    spans = re.findall(r'<span[^>]*class="blame"[^>]*title="([^"]*)"[^>]*>(.*?)</span>', html_text, re.DOTALL | re.IGNORECASE)
    if spans:
        for i, (title, content) in enumerate(spans, 1):
            rev = ""
            m = re.search(r'(?:revision|changeset):\s*([a-f0-9]+)', title, re.IGNORECASE)
            if m:
                rev = m.group(1)
            author = ""
            m = re.search(r'(?:author|user):\s*([^,]+)', title, re.IGNORECASE)
            if m:
                author = m.group(1).strip()
            date = ""
            m = re.search(r'date:\s*(.+?)(?:<br|$)', title, re.IGNORECASE)
            if m:
                date = m.group(1).strip()
            lines.append({"lineNumber": i, "revision": rev, "author": author, "date": date, "content": html.unescape(_strip(content).strip())})
        return lines
    rows = re.findall(r'<tr[^>]*>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>', html_text, re.DOTALL | re.IGNORECASE)
    for i, (rev_cell, author_cell, content_cell) in enumerate(rows, 1):
        lines.append({"lineNumber": i, "revision": _strip(rev_cell).strip(), "author": _strip(author_cell).strip(), "date": "", "content": _strip(content_cell).strip()})
    return lines