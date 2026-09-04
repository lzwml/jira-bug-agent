"""测试 OpenGrok MCP Server 的协议行为（不依赖真实 OpenGrok 实例）。"""

from __future__ import annotations

import json

import pytest

from opengrok_mcp.server import (
    TOOLS,
    _ok,
    _fail,
    _dispatch,
)
from opengrok_mcp.client import (
    _parse_search,
    _parse_projects,
    _parse_history,
    _parse_dir,
    _parse_annotate,
    _strip,
)


class FakeClient:
    """模拟 OpenGrokClient，返回预置数据。"""

    def __init__(self):
        self._base = "https://opengrok.example.com/source"

    async def search(self, **kwargs):
        return {
            "query": kwargs.get("query", ""),
            "searchType": kwargs.get("search_type", "full"),
            "totalCount": 1,
            "startIndex": 0,
            "endIndex": 1,
            "results": [
                {
                    "project": "test-project",
                    "path": "src/main.cpp",
                    "matches": [{"lineNumber": 42, "lineContent": "int main() {"}],
                }
            ],
        }

    async def get_file_content(self, project, path, **kwargs):
        return {"project": project, "path": path, "content": "test content", "lineCount": 1, "sizeBytes": 12}

    async def get_file_history(self, project, path, **kwargs):
        return {"project": project, "path": path, "entries": [{"revision": "abc123", "date": "2026-01-01", "author": "dev", "message": "fix"}]}

    async def list_projects(self):
        return [{"name": "project-a"}, {"name": "project-b"}]

    async def browse_directory(self, project, path=""):
        return [{"name": "main.cpp", "isDirectory": False, "path": "src/main.cpp"}]

    async def suggest(self, query, project=None, field="full"):
        return ["main", "main.cpp", "main_loop"]

    async def test_connection(self):
        return True

    async def get_annotate(self, project, path):
        return {"project": project, "path": path, "lines": [{"lineNumber": 1, "revision": "abc", "author": "dev", "date": "2026-01-01", "content": "code"}]}

    async def what_changed(self, project, path, since_days=14):
        return {"project": project, "path": path, "sinceDays": since_days, "recentRevisions": 1, "changes": []}


@pytest.fixture
def client():
    return FakeClient()


ANNOTATE_HTML = """<html>
<div id="src">
<pre id="src">
<span class="blame" title="revision: abc123, author: dev, date: 2026-01-01 12:00">int main() {</span>
<span class="blame" title="revision: def456, author: dev2, date: 2026-01-02 13:00">    return 0;</span>
</pre>
</div>
</html>"""


def test_parse_annotate():
    lines = _parse_annotate(ANNOTATE_HTML)
    assert len(lines) == 2
    assert lines[0]["revision"] == "abc123"
    assert lines[0]["author"] == "dev"
    assert lines[0]["lineNumber"] == 1
    assert lines[1]["revision"] == "def456"


# ============================================================
# 工具调度
# ============================================================

@pytest.mark.parametrize("tool_name,args,expected_success", [
    ("opengrok_search_code", {"query": "main"}, True),
    ("opengrok_find_file", {"path_pattern": "*.cpp"}, True),
    ("opengrok_get_file_content", {"project": "p", "path": "f.cpp"}, True),
    ("opengrok_get_file_history", {"project": "p", "path": "f.cpp"}, True),
    ("opengrok_list_projects", {}, True),
    ("opengrok_browse_directory", {"project": "p"}, True),
    ("opengrok_search_suggest", {"query": "main"}, True),
    ("opengrok_get_file_annotate", {"project": "p", "path": "f.cpp"}, True),
    ("opengrok_what_changed", {"project": "p", "path": "f.cpp"}, True),
    ("opengrok_index_health", {}, True),
])
@pytest.mark.asyncio
async def test_all_tools_return_success(client, tool_name, args, expected_success):
    result = await _dispatch(client, tool_name, args)
    assert result["success"] == expected_success


@pytest.mark.asyncio
async def test_unknown_tool(client):
    result = await _dispatch(client, "nonexistent_tool", {})
    assert result["success"] is False
    assert result["error_code"] == "UNKNOWN_TOOL"


# ============================================================
# 工具定义
# ============================================================

def test_all_tools_prefixed():
    """所有工具必须以 opengrok_ 为前缀。"""
    for name in TOOLS:
        assert name.startswith("opengrok_"), f"工具 {name} 缺少 opengrok_ 前缀"


def test_tool_count():
    """确保工具数量在预期范围内。"""
    assert 8 <= len(TOOLS) <= 14


# ============================================================
# 响应格式
# ============================================================

def test_ok_format():
    result = _ok({"key": "value"})
    assert result["success"] is True
    assert result["data"] == {"key": "value"}
    assert result["error_code"] is None
    assert result["retryable"] is False


def test_fail_format():
    result = _fail("TEST_ERROR", "test message", True)
    assert result["success"] is False
    assert result["error_code"] == "TEST_ERROR"
    assert result["error_message"] == "test message"
    assert result["retryable"] is True


# ============================================================
# HTML 解析器
# ============================================================

PROJECTS_HTML = """<html>
<select id="project">
  <optgroup label="Core">
    <option value="project-a">Project A</option>
    <option value="project-b">Project B</option>
  </optgroup>
  <option value="standalone">Standalone</option>
</select>
</html>"""

PROJECTS_HTML_FALLBACK = """<html>
<a href="/xref/project-a/">project-a</a>
<a href="/xref/project-b/x.html">project-b</a>
</html>"""

HISTORY_HTML = """<html>
<table>
<tr class="history">
  <td>abc123</td>
  <td>2026-01-01 12:00</td>
  <td>dev</td>
  <td>fix: crash on startup</td>
</tr>
<tr class="history">
  <td>def456</td>
  <td>2026-01-02 13:00</td>
  <td>dev2</td>
  <td>refactor: cleanup</td>
</tr>
</table>
</html>"""

DIRECTORY_HTML = """<html>
<table id="dirlist">
  <tr><td><a href="/source/download/test-project/main.cpp">main.cpp</a></td></tr>
  <tr><td><a href="/source/download/test-project/README.md">README.md</a></td></tr>
</table>
</html>"""


def test_parse_projects_select():
    projects = _parse_projects(PROJECTS_HTML)
    assert len(projects) == 3
    assert projects[0] == {"name": "project-a", "category": "Core"}
    assert projects[2] == {"name": "standalone"}


def test_parse_projects_fallback():
    projects = _parse_projects(PROJECTS_HTML_FALLBACK)
    assert len(projects) == 2
    assert {"name": "project-a"} in projects


def test_parse_history():
    entries = _parse_history(HISTORY_HTML, 10)
    assert len(entries) == 2
    assert entries[0]["revision"] == "abc123"
    assert entries[0]["author"] == "dev"
    assert "crash" in entries[0]["message"]


def test_parse_directory():
    entries = _parse_dir(DIRECTORY_HTML, "test-project", "")
    assert len(entries) == 2
    assert entries[0]["name"] == "main.cpp"
    assert entries[1]["name"] == "README.md"


def test_strip():
    assert _strip("<b>hello</b>") == "hello"
    assert _strip('<a href="x">link</a>') == "link"


# ============================================================
# 搜索响应解析
# ============================================================

def test_parse_search():
    data = {
        "resultCount": 2,
        "start": 0,
        "end": 2,
        "results": {
            "/project-a/src/main.cpp": [
                {"line": "int main()", "lineNumber": 42},
            ],
            "/project-b/test/test.cpp": [
                {"line": "void test()", "lineNumber": 10},
            ],
        },
    }
    result = _parse_search(data, "full", "test")
    assert result["totalCount"] == 2
    assert len(result["results"]) == 2
    assert result["results"][0]["project"] == "project-a"
    assert result["results"][0]["path"] == "src/main.cpp"
    assert result["results"][0]["matches"][0]["lineNumber"] == 42