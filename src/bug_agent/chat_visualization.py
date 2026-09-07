"""从本地 Chat Session 生成无需服务端的会话复盘页。"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path
import re
from typing import Any

from .runstore import _atomic_write
from .tool_catalog import current_fallback_catalog


def load_chat_session(session_path: Path) -> dict[str, Any]:
    path = session_path.expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 Chat Session: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("turns"), list):
        raise ValueError("不是有效的 Chat Session：缺少 turns 数组")
    return payload


def render_chat_visualization(
    session_path: Path, output_path: Path | None = None,
) -> Path:
    path = session_path.expanduser().resolve()
    session = load_chat_session(path)
    used_tool_names = {
        event.get("tool_name")
        for turn in session.get("turns", [])
        for event in turn.get("tool_events", [])
        if isinstance(event, dict) and isinstance(event.get("tool_name"), str)
    }
    session["_visualization"] = {
        "fallback_tool_catalog": current_fallback_catalog(used_tool_names),
        "fallback_notice": "当前版本后补：原会话未保存当时的工具契约，可能与运行时版本不同。",
    }
    for turn in session.get("turns", []):
        turn["_user_html"] = render_safe_markdown(str(turn.get("user_message") or ""))
        turn["_assistant_html"] = render_safe_markdown(str(turn.get("assistant_answer") or ""))
        for event in turn.get("tool_events", []):
            if isinstance(event, dict):
                event["_return_preview"] = build_return_preview(str(event.get("result") or ""))
    encoded = base64.b64encode(
        json.dumps(session, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    if output_path is None:
        state_root = path.parent.parent if path.parent.name == "chat-sessions" else path.parent
        launcher_path = state_root / "visualizations" / f"{path.stem}.html"
        site_dir = state_root / "visualizations" / path.stem
    else:
        output_path = output_path.expanduser().resolve()
        launcher_path = output_path if output_path.suffix.lower() == ".html" else None
        site_dir = output_path.with_suffix("") if launcher_path else output_path
    _atomic_write(site_dir / "session-data.js", "window.CHAT_SESSION_B64=" + json.dumps(encoded) + ";")
    _atomic_write(site_dir / "site.css", _SITE_CSS)
    _atomic_write(site_dir / "common.js", _SITE_COMMON_JS)
    for filename, page_title, page_key, body, script in _SITE_PAGES:
        html = _SITE_SHELL
        for marker, value in {
            "__PAGE_TITLE__": page_title,
            "__PAGE_KEY__": page_key,
            "__PAGE_BODY__": body,
            "__PAGE_SCRIPT__": script,
        }.items():
            html = html.replace(marker, value)
        _atomic_write(site_dir / filename, html)
    # 保留旧的单文件入口，刷新旧书签即可进入新版站点。
    if launcher_path is not None:
        relative_index = site_dir.name + "/index.html"
        _atomic_write(launcher_path, _REDIRECT_HTML.replace("__TARGET__", relative_index))
    return site_dir / "index.html"


def render_safe_markdown(value: str) -> str:
    """渲染复盘所需的 Markdown 子集；所有原始内容先转义，禁止注入 HTML。"""

    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("```"):
            language = re.sub(r"[^A-Za-z0-9_+-]", "", line[3:].strip())
            code: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            class_name = f' class="language-{language}"' if language else ""
            output.append(f"<pre><code{class_name}>{html.escape(chr(10).join(code))}</code></pre>")
            continue
        if not line.strip():
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if (
            "|" in line and index + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        ):
            headers = _table_cells(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_table_cells(lines[index]))
                index += 1
            table = ["<div class=\"table-wrap\"><table><thead><tr>"]
            table.extend(f"<th>{_render_inline(cell)}</th>" for cell in headers)
            table.append("</tr></thead><tbody>")
            for row in rows:
                table.append("<tr>")
                table.extend(
                    f"<td>{_render_inline(row[pos] if pos < len(row) else '')}</td>"
                    for pos in range(len(headers))
                )
                table.append("</tr>")
            table.append("</tbody></table></div>")
            output.append("".join(table))
            continue
        list_match = re.match(r"^\s*(?:[-*+]\s+|(\d+)[.)]\s+)(.+)$", line)
        if list_match:
            ordered = list_match.group(1) is not None
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                match = re.match(r"^\s*(?:[-*+]\s+|(\d+)[.)]\s+)(.+)$", lines[index])
                if not match or (match.group(1) is not None) != ordered:
                    break
                items.append(f"<li>{_render_inline(match.group(2))}</li>")
                index += 1
            output.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if line.lstrip().startswith(">"):
            quotes: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quotes.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            output.append("<blockquote>" + "<br>".join(_render_inline(x) for x in quotes) + "</blockquote>")
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _starts_markdown_block(lines, index):
            paragraph.append(lines[index])
            index += 1
        output.append("<p>" + "<br>".join(_render_inline(x) for x in paragraph) + "</p>")
    return "".join(output)


def _starts_markdown_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        line.startswith("```")
        or re.match(r"^(#{1,6})\s+", line)
        or re.match(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", line)
        or line.lstrip().startswith(">")
        or ("|" in line and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1]))
    )


def _render_inline(value: str) -> str:
    escaped = html.escape(value)
    code_tokens: list[str] = []

    def protect_code(match: re.Match[str]) -> str:
        code_tokens.append(f"<code>{match.group(1)}</code>")
        return f"\x00CODE{len(code_tokens) - 1}\x00"

    escaped = re.sub(r"`([^`]+)`", protect_code, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
    for pos, token in enumerate(code_tokens):
        escaped = escaped.replace(f"\x00CODE{pos}\x00", token)
    return escaped


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def build_return_preview(raw_result: str, *, max_locations: int = 50) -> dict[str, Any]:
    """从 Tool Result 提取供人工核对的路径、标识和摘录，不替代原始返回。"""
    try:
        payload = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return {"parseable": False, "locations": [], "location_count": 0, "facts": []}
    if not isinstance(payload, dict):
        return {"parseable": True, "locations": [], "location_count": 0, "facts": []}

    data = payload.get("data")
    facts: list[dict[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)) and (
                key.endswith("_count") or key.endswith("_bytes") or key in {"case_id", "name"}
            ):
                facts.append({"name": key, "value": str(value)})

    locations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    location_count = 0
    visited = 0

    def walk(value: Any) -> None:
        nonlocal location_count, visited
        if visited >= 10_000:
            return
        visited += 1
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        location = next((
            value.get(key) for key in (
                "relative_path", "member_path", "frame_path", "extracted_path", "path",
            ) if isinstance(value.get(key), str) and value.get(key)
        ), None)
        if location:
            identifier = next((
                value.get(key) for key in (
                    "evidence_id", "artifact_id", "member_id", "finding_id", "name",
                ) if value.get(key) is not None
            ), "")
            identity = (str(location), str(identifier))
            if identity not in seen:
                seen.add(identity)
                location_count += 1
                if len(locations) < max_locations:
                    line = ""
                    if value.get("line_start") is not None:
                        line = f"L{value['line_start']}"
                        if value.get("line_end") not in {None, value.get("line_start")}:
                            line += f"-{value['line_end']}"
                    details = [
                        str(part) for part in (
                            value.get("kind"),
                            f"{value['size_bytes']} bytes" if value.get("size_bytes") is not None else None,
                            line or None,
                            value.get("excerpt") or value.get("content"),
                        ) if part
                    ]
                    locations.append({
                        "location": str(location),
                        "identifier": str(identifier),
                        "details": " · ".join(details)[:1000],
                    })
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child)

    walk(data)
    return {
        "parseable": True,
        "locations": locations,
        "location_count": location_count,
        "facts": facts[:20],
    }


_REDIRECT_HTML = '''<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url=__TARGET__"><title>打开 Agent 复盘</title></head>
<body><a href="__TARGET__">打开新版 Agent 复盘</a></body></html>'''

_SITE_SHELL = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__PAGE_TITLE__</title>
<link rel="stylesheet" href="site.css"></head><body data-page="__PAGE_KEY__">
<header><div><strong id="case-title">Agent 复盘</strong><span id="case-subtitle"></span></div>
<nav><a data-page="overview" href="index.html">概览</a><a data-page="conversation" href="conversation.html">会话</a><a data-page="tools" href="tools.html">工具</a><a data-page="optimization" href="optimization.html">优化</a></nav></header>
<main>__PAGE_BODY__</main><script src="session-data.js"></script><script src="common.js"></script>
<script>__PAGE_SCRIPT__</script></body></html>'''

_SITE_CSS = r'''
:root{--bg:#f5f6f8;--card:#fff;--ink:#182230;--muted:#667085;--line:#e1e6ec;--blue:#155eef;--green:#067647;--red:#b42318;--amber:#b54708}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}header{position:sticky;top:0;z-index:5;background:#101828;color:#fff;padding:14px max(22px,calc((100% - 1180px)/2));display:flex;align-items:center;justify-content:space-between;gap:20px}header strong{font-size:18px}header span{margin-left:12px;color:#aebbd0;font-size:13px}nav{display:flex;gap:4px}nav a{color:#d7dfeb;text-decoration:none;padding:7px 13px;border-radius:7px}nav a.active{background:#fff;color:#182230}main{max-width:1180px;margin:auto;padding:24px}.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:20px;margin-bottom:18px;box-shadow:0 1px 3px #1018280d}h1{font-size:25px;margin:0 0 7px}h2{font-size:19px;margin:0 0 12px}h3{font-size:16px;margin:0}.muted{color:var(--muted)}.metrics,.routes{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric,.route{background:#f8fafc;border-radius:9px;padding:14px}.metric b{display:block;font-size:24px}.route{text-decoration:none;color:var(--ink);border:1px solid transparent}.route:hover{border-color:#9eb8f5}.route b{display:block;color:var(--blue);margin-bottom:4px}.message{word-break:break-word;background:#f8fafc;border-radius:8px;padding:13px;max-height:520px;overflow:auto}.message h1,.message h2,.message h3,.message h4{margin:16px 0 8px}.message h1{font-size:22px}.message h2{font-size:19px}.message h3{font-size:16px}.message p{margin:8px 0}.message ul,.message ol{padding-left:24px}.message blockquote{margin:10px 0;padding:7px 12px;border-left:4px solid #9eb8f5;background:#f2f6ff}.message code{font-family:ui-monospace,Consolas,monospace;background:#eaf0f6;padding:1px 4px;border-radius:4px}.message pre{background:#101828;color:#e6edf6}.message pre code{background:transparent;padding:0}.message .table-wrap{overflow:auto}.toolbar{display:grid;grid-template-columns:1fr auto;gap:10px;margin-bottom:16px}input,select{border:1px solid #cbd3de;border-radius:7px;padding:8px;background:#fff;color:var(--ink)}input[type=search]{width:100%}.turn,.tool-group{padding:0;overflow:hidden}.turn>summary,.tool-group>summary,.tool-card>summary{list-style:none;cursor:pointer;padding:15px 18px;display:grid;grid-template-columns:80px 1fr auto;gap:12px;align-items:center}.turn>summary::-webkit-details-marker,.tool-group>summary::-webkit-details-marker,.tool-card>summary::-webkit-details-marker{display:none}.turn[open]>summary,.tool-group[open]>summary,.tool-card[open]>summary{background:#f8fafc}.body{padding:0 18px 18px}.preview{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#eaf0ff;color:#1849a9;font-size:12px;margin-left:5px}.tag.warn{background:#fff0df;color:var(--amber)}.tag.ok{background:#ecfdf3;color:var(--green)}.tag.bad{background:#fff1f0;color:var(--red)}.tool-card{border-top:1px solid var(--line)}.tool-card:first-child{border-top:0}.tool-card>summary{grid-template-columns:160px 1fr auto}.purpose{margin:8px 0;color:#344054}.notice{font-size:12px;color:var(--amber)}table{width:100%;border-collapse:collapse;margin:9px 0;font-size:13px}th,td{text-align:left;vertical-align:top;border:1px solid #e6e9ee;padding:7px;word-break:break-word}th{background:#f8fafc}.mono{font-family:ui-monospace,Consolas,monospace}.result{padding:9px 11px;border-radius:7px;background:#f0fdf4;color:#05603a;margin:8px 0}.result.bad{background:#fff1f0;color:var(--red)}details.raw summary{cursor:pointer;color:var(--blue)}pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:10px;border-radius:7px;max-height:380px;overflow:auto}.review{display:grid;grid-template-columns:auto 170px 170px 1fr;gap:8px;align-items:center}.review input[type=text]{width:100%}button{border:0;border-radius:8px;padding:9px 14px;background:var(--blue);color:#fff;font-weight:650;cursor:pointer}.row{display:flex;justify-content:space-between;gap:12px;align-items:center}.hidden{display:none!important}.empty{text-align:center;color:var(--muted);padding:28px}@media(max-width:780px){header{position:static;display:block}nav{margin-top:10px}.metrics,.routes{grid-template-columns:1fr 1fr}.review,.toolbar{grid-template-columns:1fr}.turn>summary,.tool-group>summary,.tool-card>summary{grid-template-columns:65px 1fr}.turn>summary>span:last-child,.tool-group>summary>span:last-child,.tool-card>summary>span:last-child{grid-column:2}header span{display:block;margin:2px 0}}
'''

_SITE_COMMON_JS = r'''
const session=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(window.CHAT_SESSION_B64),c=>c.charCodeAt(0))));
const turns=Array.isArray(session.turns)?session.turns:[],task=session.task||{};
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n};
const markdown=(markup)=>{const n=el('div','message');n.innerHTML=markup||'<p>（空）</p>';return n};
document.getElementById('case-title').textContent=task.issue_key||session.session_id||'Agent 复盘';document.getElementById('case-subtitle').textContent=task.objective||'';document.querySelector(`nav a[data-page="${document.body.dataset.page}"]`)?.classList.add('active');
const catalogs=new Map((session.tool_catalogs||[]).map(x=>[x.catalog_id,x.tools||[]])),fallback=session._visualization?.fallback_tool_catalog||[];
const catalogFor=t=>{const exact=catalogs.get(t.tool_catalog_id);return{tools:exact||fallback,exact:!!exact}},toolDef=(t,name)=>catalogFor(t).tools.find(x=>x.name===name)||{name,description:'没有可用的工具描述。',parameters:{type:'object',properties:{}}};
const allEvents=turns.flatMap(t=>(t.tool_events||[]).map(x=>({turn:t,event:x}))),short=v=>{const s=typeof v==='string'?v:JSON.stringify(v);return s.length>280?s.slice(0,280)+'…':s};
function resultSummary(raw){try{const p=JSON.parse(raw);if(p.success===false)return{ok:false,text:`返回失败：${p.error_code||'UNKNOWN'} · ${p.error_message||'未提供原因'}`};const d=p.data;if(Array.isArray(d))return{ok:true,text:`返回成功：${d.length} 项数据`};if(d&&typeof d==='object')return{ok:true,text:`返回成功；主要内容：${Object.keys(d).slice(0,8).join('、')||'无字段'}`};return{ok:true,text:'返回成功'}}catch{return{ok:false,text:'返回不是标准 JSON，需要查看原始内容'}}}
function appendToolDetails(root,t,x,withReview=false){const def=toolDef(t,x.tool_name),schema=def.parameters||{},props=schema.properties||{},required=schema.required||[],args=x.arguments||{},missing=required.filter(k=>!(k in args)),exact=catalogFor(t).exact;root.append(el('div','purpose',def.description||'未提供工具用途说明'));const badges=el('div');badges.append(el('span','tag',exact?'运行时契约快照':'当前版本后补描述'),el('span',`tag ${missing.length?'bad':'ok'}`,missing.length?`缺少：${missing.join('、')}`:'必填参数齐全'));root.append(badges);if(!exact)root.append(el('div','notice',session._visualization?.fallback_notice||''));const table=el('table'),head=el('tr');['参数','本次传值','契约说明'].forEach(v=>head.append(el('th','',v)));table.append(head);[...new Set([...Object.keys(args),...missing])].forEach(k=>{const row=el('tr');row.append(el('td','mono',k+(required.includes(k)?' *':'')),el('td','',k in args?short(args[k]):'未传'),el('td','',props[k]?.description||`类型：${props[k]?.type||'未说明'}`));table.append(row)});root.append(table);const rs=resultSummary(x.result||'');root.append(el('div',`result ${rs.ok?'':'bad'}`,rs.text));const p=x._return_preview||{},loc=p.locations||[];if(loc.length){root.append(el('h3','',`可核对的文件/证据位置（${p.location_count} 项）`));const rt=el('table'),rh=el('tr');['位置','稳定标识','类型、大小、行号或摘录'].forEach(v=>rh.append(el('th','',v)));rt.append(rh);loc.forEach(v=>{const row=el('tr');row.append(el('td','mono',v.location),el('td','mono',v.identifier||'-'),el('td','',v.details||'-'));rt.append(row)});root.append(rt)}else root.append(el('div','notice','返回没有携带可定位的文件路径或 Evidence 位置。'));if(withReview){const review=el('div','review'),select=document.createElement('select'),note=document.createElement('input');select.className='tool-verdict';[['','返回是否满足需要'],['sufficient','满足'],['partial','部分满足'],['insufficient','不满足'],['wrong_arguments','参数不正确'],['wrong_tool','工具选择错误']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;select.append(o)});note.type='text';note.className='tool-note';note.placeholder='说明缺失内容或正确调用方式';review.append(el('span','',x.tool_name),select,note);review.dataset.turn=t.turn_index;review.dataset.step=x.step||'';review.dataset.call=x.tool_call_id||'';root.append(review)}const raw=el('details','raw'),sum=el('summary','','查看完整原始参数与返回'),pre=el('pre','mono',`参数\n${JSON.stringify(args,null,2)}\n\n返回\n${x.result||''}`);raw.append(sum,pre);root.append(raw)}
'''

_OVERVIEW_BODY = '''<section class="card"><h1>复盘概览</h1><p class="muted">先判断结果，再选择要深入的页面。</p><div class="metrics" id="metrics"></div></section>
<section class="card"><h2>当前结论</h2><div class="message" id="latest"></div></section>
<section class="routes"><a class="route" href="conversation.html"><b>阅读会话</b>只看人工输入和 Agent 回答</a><a class="route" href="tools.html"><b>核对工具</b>检查用途、参数和返回文件</a><a class="route" href="optimization.html"><b>标注优化</b>确认哪些人工参与应进入优化</a></section>'''
_OVERVIEW_JS = r'''const names=[...new Set(allEvents.map(x=>x.event.tool_name))];[['会话轮次',turns.length],['工具调用',allEvents.length],['工具种类',names.length],['人工参与',Math.max(0,turns.length-1)]].forEach(([k,v])=>{const m=el('div','metric');m.append(el('b','',v),el('span','',k));document.getElementById('metrics').append(m)});const latest=turns.at(-1);document.getElementById('latest').replaceWith(markdown(latest?._assistant_html||''));'''

_CONVERSATION_BODY = '''<section><h1>会话过程</h1><p class="muted">这里只阅读人工输入与 Agent 回答；工具细节统一到“工具”页核对。</p><div class="toolbar"><input id="search" type="search" placeholder="搜索会话内容"><span></span></div><div id="turns"></div></section>'''
_CONVERSATION_JS = r'''const root=document.getElementById('turns');turns.forEach((t,i)=>{const d=el('details','card turn');d.id=`turn-${t.turn_index}`;d.dataset.search=((t.user_message||'')+'\n'+(t.assistant_answer||'')).toLowerCase();d.open=i===turns.length-1;const s=el('summary');s.append(el('strong','',`第 ${t.turn_index} 轮`),el('span','preview',(t.user_message||'').replace(/\s+/g,' ').slice(0,110)),el('span','tag',i===0?'任务设定':'人工引导'));d.append(s);const b=el('div','body');b.append(el('h3','','人工输入'),markdown(t._user_html),el('h3','','Agent 回答'),markdown(t._assistant_html));if((t.tool_events||[]).length){const a=el('a','',`本轮有 ${t.tool_events.length} 次工具调用，前往工具页核对 →`);a.href=`tools.html#turn-${t.turn_index}`;b.append(a)}d.append(b);root.append(d)});document.getElementById('search').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('.turn').forEach(x=>x.classList.toggle('hidden',q&&!x.dataset.search.includes(q)))};'''

_TOOLS_BODY = '''<section><h1>工具调用</h1><p class="muted">集中核对工具是否选对、参数是否符合契约，以及返回中是否包含所需文件或 Evidence。</p><div class="toolbar"><input id="search" type="search" placeholder="搜索工具名、参数或返回路径"><select id="tool-filter"><option value="">全部工具</option></select></div><div id="groups"></div></section>'''
_TOOLS_JS = r'''const names=[...new Set(allEvents.map(x=>x.event.tool_name))].sort(),select=document.getElementById('tool-filter');names.forEach(n=>{const o=el('option','',n);o.value=n;select.append(o)});const root=document.getElementById('groups');turns.filter(t=>(t.tool_events||[]).length).forEach(t=>{const group=el('details','card tool-group');group.id=`turn-${t.turn_index}`;const s=el('summary');s.append(el('strong','',`第 ${t.turn_index} 轮`),el('span','preview',(t.user_message||'').replace(/\s+/g,' ').slice(0,100)),el('span','tag',`${t.tool_events.length} 次调用`));group.append(s);const body=el('div','body');t.tool_events.forEach(x=>{const d=el('details','tool-card');d.dataset.name=x.tool_name;d.dataset.search=(x.tool_name+' '+JSON.stringify(x.arguments||{})+' '+JSON.stringify(x._return_preview||{})).toLowerCase();const h=el('summary');h.append(el('strong','',x.tool_name),el('span','preview',toolDef(t,x.tool_name).description),el('span',`tag ${x.success?'ok':'bad'}`,x.success?'成功':'失败'));d.append(h);const content=el('div','body');appendToolDetails(content,t,x,false);d.append(content);body.append(d)});group.append(body);root.append(group)});function apply(){const q=document.getElementById('search').value.toLowerCase(),name=select.value;document.querySelectorAll('.tool-card').forEach(x=>x.classList.toggle('hidden',(q&&!x.dataset.search.includes(q))||(name&&x.dataset.name!==name)));document.querySelectorAll('.tool-group').forEach(g=>g.classList.toggle('hidden',![...g.querySelectorAll('.tool-card')].some(x=>!x.classList.contains('hidden'))))}document.getElementById('search').oninput=apply;select.onchange=apply;if(location.hash)document.querySelector(location.hash)?.setAttribute('open','');'''

_OPTIMIZATION_BODY = '''<section><div class="row"><div><h1>Agent 优化标注</h1><p class="muted">只在这里确认人工参与和工具返回是否应进入优化样本。</p></div><button id="export">导出复盘结论</button></div><div id="reviews"></div></section>'''
_OPTIMIZATION_JS = r'''const root=document.getElementById('reviews');turns.forEach((t,i)=>{const d=el('details','card turn'),s=el('summary');d.dataset.turn=t.turn_index;s.append(el('strong','',`第 ${t.turn_index} 轮`),el('span','preview',(t.user_message||'').replace(/\s+/g,' ').slice(0,110)),el('span','tag',i===0?'任务设定':'人工引导'));d.append(s);const b=el('div','body'),review=el('div','review'),label=el('label'),check=document.createElement('input');check.type='checkbox';check.className='mark';check.checked=i>0;label.append(check,document.createTextNode(' 纳入优化'));const kind=document.createElement('select');kind.className='kind';[['task_definition','任务设定'],['context_supply','补充上下文'],['scope_refinement','调整范围'],['evidence_direction','指定证据'],['hypothesis_challenge','挑战假设'],['fact_correction','纠正事实'],['request_deeper','继续深挖'],['confirmation','确认结论']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;kind.append(o)});kind.value=i===0?'task_definition':(t.tool_events||[]).length?'evidence_direction':'request_deeper';const target=document.createElement('select');target.className='target';[['tool_selection','工具选择'],['skill_routing','Skill 路由'],['skill_content','Skill 内容'],['case_context','Case 输入'],['unknown','暂不确定']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;target.append(o)});target.value=(t.tool_events||[]).length?'tool_selection':'unknown';const note=document.createElement('input');note.type='text';note.className='note';note.placeholder='这轮暴露了 Agent 的什么不足';review.append(label,kind,target,note);b.append(el('div','message',t.user_message||''),review);(t.tool_events||[]).forEach(x=>{const tool=el('details','tool-card'),h=el('summary');h.append(el('strong','',x.tool_name),el('span','preview',toolDef(t,x.tool_name).description),el('span',`tag ${x.success?'ok':'bad'}`,x.success?'成功':'失败'));tool.append(h);const c=el('div','body');appendToolDetails(c,t,x,true);tool.append(c);b.append(tool)});d.append(b);root.append(d)});document.getElementById('export').onclick=()=>{const annotations=[...document.querySelectorAll('.turn')].map(c=>{if(!c.querySelector('.mark').checked)return null;const t=turns.find(x=>String(x.turn_index)===c.dataset.turn);return{turn_index:t.turn_index,interaction_kind:c.querySelector('.kind').value,target:c.querySelector('.target').value,note:c.querySelector('.note').value.trim(),human_feedback:t.user_message,subsequent_tools:(t.tool_events||[]).map(x=>x.tool_name)}}).filter(Boolean),tool_reviews=[...document.querySelectorAll('.review[data-step]')].map(c=>{const verdict=c.querySelector('.tool-verdict').value;if(!verdict)return null;return{turn_index:Number(c.dataset.turn),step:Number(c.dataset.step),tool_call_id:c.dataset.call||null,tool_name:c.firstChild?.textContent||'',verdict,note:c.querySelector('.tool-note').value.trim()}}).filter(Boolean),out={schema_version:2,record_kind:'chat_review',session_id:session.session_id,source_updated_at:session.updated_at,annotations,tool_reviews};const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`chat-review-${session.session_id}.json`;a.click();URL.revokeObjectURL(a.href)};'''

_SITE_PAGES = (
    ("index.html", "Agent 复盘概览", "overview", _OVERVIEW_BODY, _OVERVIEW_JS),
    ("conversation.html", "Agent 会话过程", "conversation", _CONVERSATION_BODY, _CONVERSATION_JS),
    ("tools.html", "Agent 工具调用", "tools", _TOOLS_BODY, _TOOLS_JS),
    ("optimization.html", "Agent 优化标注", "optimization", _OPTIMIZATION_BODY, _OPTIMIZATION_JS),
)


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent 问题复盘</title><style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#182230;--muted:#667085;--line:#e3e7ec;--blue:#155eef;--green:#067647;--red:#b42318;--amber:#b54708;--amberbg:#fff8eb;--purple:#6938ef}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}.hero{padding:28px max(24px,calc((100% - 1160px)/2));background:#101828;color:#fff}.hero h1{margin:0 0 5px;font-size:26px}.sub{color:#cbd5e1}.wrap{max-width:1160px;margin:auto;padding:22px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 1px 3px #1018280d}h2{font-size:19px;margin:0 0 14px}h3{font-size:16px;margin:0}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.metric{background:#f8fafc;border-radius:10px;padding:14px}.metric b{display:block;font-size:24px}.metric span,.muted{color:var(--muted)}.summary{white-space:pre-wrap;word-break:break-word;max-height:460px;overflow:auto;padding:14px;background:#f8fafc;border-radius:9px}.feedback{border-left:4px solid var(--amber);background:var(--amberbg)}.feedback-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.bubble{white-space:pre-wrap;word-break:break-word;background:#fff;border:1px solid #f1d3a7;border-radius:9px;padding:12px;max-height:300px;overflow:auto}.label{font-weight:700;margin-bottom:5px}.review-row{display:grid;grid-template-columns:auto 155px 155px 1fr;gap:9px;align-items:center;margin-top:12px}.review-row input[type=text]{width:100%}.toolbar{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center}.history{padding:0;overflow:hidden}.turn{border-top:1px solid var(--line)}.turn:first-child{border-top:0}.turn>summary{list-style:none;cursor:pointer;padding:16px 20px;display:grid;grid-template-columns:78px 1fr auto;gap:12px;align-items:center}.turn>summary::-webkit-details-marker{display:none}.turn[open]>summary{background:#f8fafc}.turn-body{padding:0 20px 20px}.preview{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#eaf0ff;color:#1849a9;font-size:12px;margin-left:5px}.tag.warn{background:#fff0df;color:var(--amber)}.tag.purple{background:#f0eaff;color:var(--purple)}.message{white-space:pre-wrap;word-break:break-word;background:#f8fafc;border-radius:8px;padding:12px;max-height:440px;overflow:auto}.tool-list{margin-top:12px}.tool{padding:13px 0;border-top:1px solid #dfe5ec}.tool-head{display:flex;justify-content:space-between;gap:12px}.tool-purpose{margin:7px 0;color:#344054}.contract{display:flex;gap:8px;align-items:center;margin:8px 0}.contract .tag{margin-left:0}.param-table,.result-table{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}.param-table th,.param-table td,.result-table th,.result-table td{text-align:left;vertical-align:top;border:1px solid #e6e9ee;padding:7px;word-break:break-word}.param-table th,.result-table th{background:#f8fafc}.param-table td:nth-child(1){width:17%;font-family:ui-monospace,Consolas,monospace}.param-table td:nth-child(2){width:31%}.result-table td:first-child{width:38%;font-family:ui-monospace,Consolas,monospace}.result-table td:nth-child(2){width:23%;font-family:ui-monospace,Consolas,monospace}.return-summary{padding:9px 11px;border-radius:7px;background:#f0fdf4;color:#05603a;margin:8px 0}.return-summary.fail{background:#fff1f0;color:var(--red)}.result-facts{font-size:12px;color:var(--muted);margin:6px 0}.catalog-notice{font-size:12px;color:var(--amber)}.tool-review{display:grid;grid-template-columns:210px 1fr;gap:8px;margin:10px 0;padding:10px;background:#f5f8ff;border-radius:8px}.tool-review input{width:100%}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:10px;border-radius:7px;max-height:360px;overflow:auto}details details{margin-top:7px}details details summary{cursor:pointer;color:var(--blue)}input,select{border:1px solid #cfd6df;border-radius:7px;padding:8px;background:#fff;color:var(--ink)}input[type=search]{width:100%}button{border:0;border-radius:8px;padding:9px 14px;background:var(--blue);color:#fff;font-weight:650;cursor:pointer}.empty{padding:18px;color:var(--muted);text-align:center}.hidden{display:none!important}.ok{color:var(--green)}.bad{color:var(--red)}@media(max-width:800px){.metrics,.feedback-grid{grid-template-columns:1fr 1fr}.toolbar,.review-row,.tool-review{grid-template-columns:1fr}.turn>summary{grid-template-columns:65px 1fr}.turn>summary>span:last-child{grid-column:2}.param-table,.result-table{display:block;overflow:auto}}
</style></head><body><header class="hero"><h1 id="title">Agent 问题复盘</h1><div class="sub" id="subtitle"></div></header><main class="wrap">
<section class="card"><h2>先看结果</h2><div class="metrics" id="metrics"></div><div class="label" style="margin-top:16px">当前结论（最后一轮）</div><div class="summary" id="latest"></div></section>
<section class="card toolbar"><input type="search" id="search" placeholder="搜索完整过程"><label><input type="checkbox" id="toolsOnly"> 只看调用过工具的轮次</label><button id="export">导出复盘结论</button></section>
<section><h2>完整过程</h2><p class="muted">每轮都是一次“人工输入 → Agent 响应”。默认折叠；展开某轮后再核对工具用途、参数契约和返回是否满足需要。</p><div class="card history" id="history"></div></section>
</main><script>
const session=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('__CHAT_SESSION_BASE64__'),c=>c.charCodeAt(0))));
const turns=Array.isArray(session.turns)?session.turns:[],task=session.task||{},el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n};
const catalogs=new Map((session.tool_catalogs||[]).map(x=>[x.catalog_id,x.tools||[]])),fallback=session._visualization?.fallback_tool_catalog||[];
const catalogFor=t=>{const exact=catalogs.get(t.tool_catalog_id);return{tools:exact||fallback,exact:!!exact}},toolDef=(t,name)=>catalogFor(t).tools.find(x=>x.name===name)||{name,description:'没有可用的工具描述。',parameters:{type:'object',properties:{}}};
const shortValue=v=>{const raw=typeof v==='string'?v:JSON.stringify(v);return raw.length>260?raw.slice(0,260)+'…':raw};
function resultSummary(raw){try{const p=JSON.parse(raw);if(p.success===false)return{ok:false,text:`返回失败：${p.error_code||'UNKNOWN'} · ${p.error_message||'未提供原因'}`};const d=p.data;if(Array.isArray(d))return{ok:true,text:`返回成功：${d.length} 项数据`};if(d&&typeof d==='object'){const keys=Object.keys(d);return{ok:true,text:`返回成功；主要内容：${keys.slice(0,8).join('、')||'无字段'}`}}return{ok:true,text:'返回成功'}}catch{return{ok:false,text:'返回不是标准 JSON，需要人工查看原始内容'}}}
function appendToolContract(container,t,x){const def=toolDef(t,x.tool_name),schema=def.parameters||{},props=schema.properties||{},required=schema.required||[],args=x.arguments||{},missing=required.filter(k=>!(k in args)),unknown=Object.keys(args).filter(k=>!(k in props)),origin=catalogFor(t).exact?'运行时契约快照':'当前版本后补描述';container.append(el('div','tool-purpose',def.description||'未提供工具用途说明'));const state=el('div','contract');state.append(el('span','tag',origin),el('span',missing.length?'bad':'ok',missing.length?`缺少必填参数：${missing.join('、')}`:'必填参数齐全'));if(unknown.length&&schema.additionalProperties===false)state.append(el('span','bad',`契约外参数：${unknown.join('、')}`));container.append(state);if(!catalogFor(t).exact)container.append(el('div','catalog-notice',session._visualization?.fallback_notice||''));const table=el('table','param-table'),thead=el('thead'),hr=el('tr');['参数','本次传值','契约说明'].forEach(v=>hr.append(el('th','',v)));thead.append(hr);const tbody=el('tbody');[...new Set([...Object.keys(args),...missing])].forEach(k=>{const row=el('tr');row.append(el('td','',k+(required.includes(k)?' *':'')),el('td',k in args?'':'bad',k in args?shortValue(args[k]):'未传'),el('td','',props[k]?.description||`类型：${props[k]?.type||'未说明'}`));tbody.append(row)});table.append(thead,tbody);container.append(table);const rs=resultSummary(x.result||''),summary=el('div','return-summary'+(rs.ok?'':' fail'),rs.text);container.append(summary);const preview=x._return_preview||{},locations=preview.locations||[],facts=preview.facts||[];if(facts.length)container.append(el('div','result-facts','返回概况：'+facts.map(v=>`${v.name}=${v.value}`).join(' · ')));if(locations.length){container.append(el('div','label',`可人工核对的文件/证据位置（${preview.location_count} 项${preview.location_count>locations.length?'，当前显示前 '+locations.length+' 项':''}）`));const rt=el('table','result-table'),rh=el('tr');['文件或证据位置','稳定标识','类型、大小、行号或摘录'].forEach(v=>rh.append(el('th','',v)));const rhead=el('thead');rhead.append(rh);const rb=el('tbody');locations.forEach(v=>{const row=el('tr');row.append(el('td','',v.location),el('td','',v.identifier||'-'),el('td','',v.details||'-'));rb.append(row)});rt.append(rhead,rb);container.append(rt)}else container.append(el('div','catalog-notice','该返回没有携带可定位的文件路径或 Evidence 位置；请结合工具用途判断这是否为缺口。'));const review=el('div','tool-review'),verdict=document.createElement('select'),note=document.createElement('input');verdict.className='tool-verdict';[['','人工确认：返回是否满足需要'],['sufficient','满足本轮需要'],['partial','只满足一部分'],['insufficient','没有返回所需信息'],['wrong_arguments','参数不正确'],['wrong_tool','工具选择错误']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;verdict.append(o)});note.type='text';note.className='tool-note';note.placeholder='说明缺了什么，或哪个参数/工具更合适';review.append(verdict,note);container.append(review);}
const correctionUser=/(不对|不是这样|两码事|无关|你认可吗|纠正|更正|应该是|我认为)/i,correctionAgent=/(你说得对|我认可|之前的错误|重新梳理|需要纠正|判断有误)/i;
const isExplicitCorrection=t=>!!t.human_intervention||(correctionUser.test(t.user_message||'')&&correctionAgent.test(t.assistant_answer||''));
const allEvents=turns.flatMap(t=>t.tool_events||[]),usedTools=[...new Set(allEvents.map(x=>x.tool_name))];
document.getElementById('title').textContent=task.issue_key?`${task.issue_key} · Agent 问题复盘`:'Agent 问题复盘';document.getElementById('subtitle').textContent=`${task.objective||''} · ${session.status==='closed'?'会话已结束':'会话进行中'}`;
[['人工输入',turns.length],['后续调查动作',allEvents.length],['使用工具',usedTools.length],['显式纠错',turns.filter(isExplicitCorrection).length]].forEach(([k,v])=>{const box=el('div','metric');box.append(el('b','',v),el('span','',k));document.getElementById('metrics').append(box)});
document.getElementById('latest').textContent=turns.at(-1)?.assistant_answer||'尚无结论';
const history=document.getElementById('history');turns.forEach((t,i)=>{const events=t.tool_events||[],explicit=isExplicitCorrection(t),d=el('details','turn');d.dataset.turn=String(t.turn_index);d.dataset.search=[t.user_message,t.assistant_answer,...events.map(x=>x.tool_name)].join('\n').toLowerCase();d.dataset.tools=events.length?'1':'0';d.open=i===turns.length-1||explicit;const s=el('summary'),index=el('strong','',`第 ${t.turn_index} 轮`),preview=el('span','preview',(t.user_message||'').replace(/\s+/g,' ').slice(0,100)||'（无用户输入）'),tags=el('span');if(events.length)tags.append(el('span','tag',`${events.length} 次调查`));if(i>0)tags.append(el('span','tag warn','人工引导'));if(t.human_checkpoint)tags.append(el('span','tag purple','请求人工提示'));if(explicit)tags.append(el('span','tag warn','显式纠错'));s.append(index,preview,tags);d.append(s);const body=el('div','turn-body');body.append(el('div','label','用户'),el('div','message',t.user_message||'（空）'),el('div','label','Agent'),el('div','message',t.assistant_answer||'（空）'));const row=el('div','review-row'),mark=el('label'),check=document.createElement('input');check.type='checkbox';check.className='mark';check.checked=i>0;mark.append(check,document.createTextNode(' 纳入 Agent 优化样本'));const kind=document.createElement('select');kind.className='kind';[['task_definition','任务设定'],['context_supply','补充上下文'],['scope_refinement','调整调查范围'],['evidence_direction','指定证据方向'],['hypothesis_challenge','挑战已有假设'],['fact_correction','纠正事实/概念'],['request_deeper','要求继续深挖'],['confirmation','确认当前结论']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;kind.append(o)});kind.value=i===0?'task_definition':explicit?'fact_correction':events.length?'evidence_direction':'request_deeper';const target=document.createElement('select');target.className='target';[['tool_selection','工具选择'],['skill_routing','Skill 路由'],['skill_content','Skill 内容'],['case_context','Case 输入'],['unknown','暂不确定']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;target.append(o)});target.value=t.human_intervention?.attribution?.target||(events.some(x=>x.tool_name==='activate_skill')?'skill_routing':events.length?'tool_selection':'unknown');const note=document.createElement('input');note.type='text';note.className='note';note.placeholder='这一轮暴露了 Agent 的什么不足';row.append(mark,kind,target,note);body.append(row);
 if(events.length){const list=el('details','tool-list'),ls=el('summary','',`查看 ${events.length} 次工具调用与参数契约`);list.append(ls);events.forEach(x=>{const tool=el('div','tool'),h=el('div','tool-head');tool.dataset.turn=String(t.turn_index);tool.dataset.call=x.tool_call_id||'';tool.dataset.step=String(x.step??'');tool.dataset.name=x.tool_name||'unknown';h.append(el('strong','',x.tool_name||'unknown'),el('span',x.success?'ok':'bad',x.success?'调用成功':'调用失败'));tool.append(h);appendToolContract(tool,t,x);const raw=el('details'),rs=el('summary','','查看完整原始参数与返回'),pre=el('div','mono',`参数\n${JSON.stringify(x.arguments||{},null,2)}\n\n返回\n${x.result||''}`);raw.append(rs,pre);tool.append(raw);list.append(tool)});body.append(list)}d.append(body);history.append(d)});
function filter(){const q=document.getElementById('search').value.trim().toLowerCase(),tools=document.getElementById('toolsOnly').checked;document.querySelectorAll('.turn').forEach(x=>x.classList.toggle('hidden',!!((q&&!x.dataset.search.includes(q))||(tools&&x.dataset.tools!=='1'))))}document.getElementById('search').oninput=filter;document.getElementById('toolsOnly').onchange=filter;
document.getElementById('export').onclick=()=>{const cards=[...document.querySelectorAll('.turn')],annotations=cards.map(c=>{if(!c.querySelector('.mark').checked)return null;const turn=turns.find(t=>String(t.turn_index)===c.dataset.turn);return{turn_index:turn.turn_index,interaction_kind:c.querySelector('.kind').value,target:c.querySelector('.target').value,note:c.querySelector('.note').value.trim(),human_feedback:turn.human_intervention?.response||turn.user_message,structured_intervention:!!turn.human_intervention,subsequent_tools:(turn.tool_events||[]).map(x=>x.tool_name)}}).filter(Boolean),tool_reviews=[...document.querySelectorAll('.tool')].map(c=>{const verdict=c.querySelector('.tool-verdict').value;if(!verdict)return null;return{turn_index:Number(c.dataset.turn),step:Number(c.dataset.step),tool_call_id:c.dataset.call||null,tool_name:c.dataset.name,verdict,note:c.querySelector('.tool-note').value.trim()}}).filter(Boolean),out={schema_version:2,record_kind:'chat_review',session_id:session.session_id,source_updated_at:session.updated_at,annotations,tool_reviews};const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`chat-review-${session.session_id||'session'}.json`;a.click();URL.revokeObjectURL(a.href)};
</script></body></html>'''
