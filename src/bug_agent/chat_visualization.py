"""从本地 Chat Session 生成无需服务端的会话复盘页。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .runstore import _atomic_write


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
    encoded = base64.b64encode(
        json.dumps(session, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    if output_path is None:
        state_root = path.parent.parent if path.parent.name == "chat-sessions" else path.parent
        output_path = state_root / "visualizations" / f"{path.stem}.html"
    else:
        output_path = output_path.expanduser().resolve()
    _atomic_write(output_path, _HTML.replace("__CHAT_SESSION_BASE64__", encoded))
    return output_path


_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>稳定性 Agent 会话复盘</title><style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#17202a;--muted:#667085;--line:#dfe4ea;--blue:#155eef;--green:#067647;--red:#b42318;--amber:#b54708;--purple:#6938ef}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}.top{padding:24px 30px;background:#101828;color:white}.top h1{margin:0 0 7px;font-size:24px}.sub{color:#cbd5e1}.wrap{max-width:1500px;margin:auto;padding:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px #1018280d}.metrics{display:grid;grid-template-columns:repeat(6,minmax(100px,1fr));gap:10px}.metric{background:#f8fafc;border-radius:9px;padding:12px}.metric b{display:block;font-size:21px}.metric span,.muted{color:var(--muted)}.controls{display:grid;grid-template-columns:minmax(220px,1fr) repeat(3,auto);gap:10px;align-items:center;position:sticky;top:0;z-index:3}.controls input{min-width:0}.turn{border-left:4px solid var(--blue)}.turn.feedback{border-color:var(--amber)}.turn.checkpoint{border-color:var(--purple)}.head,.tool-head,.actions{display:flex;align-items:center;justify-content:space-between;gap:12px}.head h2{font-size:17px;margin:0}.bad{color:var(--red)}.ok{color:var(--green)}.tag{display:inline-block;padding:2px 8px;border-radius:999px;background:#eaf0ff;color:#1849a9;font-size:12px;margin-left:6px}.tag.feedback{background:#fff0e5;color:var(--amber)}.tag.checkpoint{background:#f0eaff;color:var(--purple)}.message{white-space:pre-wrap;word-break:break-word;background:#f8fafc;border-radius:8px;padding:12px;max-height:520px;overflow:auto}.label{font-weight:700;margin:14px 0 6px}.tools{margin-top:12px}.tool{padding:10px 0;border-top:1px solid #edf0f3}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:10px;border-radius:7px;max-height:420px;overflow:auto}details{margin-top:7px}summary{cursor:pointer;color:var(--blue)}input,select,textarea{border:1px solid #cfd6df;border-radius:7px;padding:8px;background:white;color:var(--ink)}input[type=search]{width:100%}button{border:0;border-radius:8px;padding:9px 13px;background:var(--blue);color:white;font-weight:650;cursor:pointer}.secondary{background:#eef2f6;color:#344054}.annotation{margin-top:12px;padding:10px;border:1px dashed #f0b36d;border-radius:8px;background:#fffaf5}.annotation .row{display:grid;grid-template-columns:auto 180px 1fr;gap:8px;align-items:center}.annotation input[type=text]{width:100%}.hidden{display:none!important}@media(max-width:900px){.metrics{grid-template-columns:repeat(2,1fr)}.controls{grid-template-columns:1fr 1fr;position:static}.annotation .row{grid-template-columns:1fr}.head{align-items:flex-start;flex-direction:column}}
</style></head><body><header class="top"><h1>稳定性 Agent 会话复盘</h1><div class="sub" id="subtitle"></div></header><main class="wrap">
<section class="card"><div class="metrics" id="metrics"></div></section>
<section class="card controls"><input type="search" id="search" placeholder="搜索用户输入、Agent 回答或工具名"><label><input type="checkbox" id="toolsOnly"> 有工具调用</label><label><input type="checkbox" id="feedbackOnly"> 人工反馈</label><button id="export">导出复盘标注</button></section>
<section id="turns"></section></main><script>
const session=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('__CHAT_SESSION_BASE64__'),c=>c.charCodeAt(0))));
const turns=Array.isArray(session.turns)?session.turns:[];const task=session.task||{};const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n};
const correctionUser=/(不对|不是这样|两码事|无关|你认可吗|纠正|更正|应该是|我认为)/i;const correctionAgent=/(你说得对|我认可|之前的错误|重新梳理|需要纠正|判断有误)/i;
const isLegacyFeedback=t=>!t.human_intervention&&correctionUser.test(t.user_message||'')&&correctionAgent.test(t.assistant_answer||'');
const toolCount=turns.reduce((n,t)=>n+(t.tool_events||[]).length,0),failures=turns.reduce((n,t)=>n+(t.tool_events||[]).filter(x=>!x.success).length,0),checkpoints=turns.filter(t=>t.human_checkpoint).length,interventions=turns.filter(t=>t.human_intervention).length,legacy=turns.filter(isLegacyFeedback).length;
document.getElementById('subtitle').textContent=`${session.session_id||''} · ${task.issue_key||task.case_path||''} · ${task.objective||''}`;
[['状态',session.status||'unknown'],['会话轮次',turns.length],['工具调用',toolCount],['失败调用',failures],['人工检查点',checkpoints],['纠偏候选',interventions+legacy]].forEach(([k,v])=>{const box=el('div','metric');box.append(el('b','',v),el('span','',k));document.getElementById('metrics').append(box)});
const root=document.getElementById('turns');
turns.forEach(t=>{const structured=!!t.human_intervention,possible=isLegacyFeedback(t),checkpoint=!!t.human_checkpoint,events=t.tool_events||[];const card=el('article','card turn'+((structured||possible)?' feedback':'')+(checkpoint?' checkpoint':''));card.dataset.search=[t.user_message,t.assistant_answer,...events.map(x=>x.tool_name)].join('\n').toLowerCase();card.dataset.tools=events.length?'1':'0';card.dataset.feedback=(structured||possible||checkpoint)?'1':'0';
 const head=el('div','head'),title=el('h2','',`第 ${t.turn_index} 轮`),tags=el('div');tags.append(el('span','tag',`${t.agent_status||'unknown'} · ${t.steps||0} 步`));if(structured)tags.append(el('span','tag feedback','结构化人工介入'));else if(possible)tags.append(el('span','tag feedback','可能的人工纠偏 · 待确认'));if(checkpoint)tags.append(el('span','tag checkpoint','等待人工提示'));head.append(title,tags);card.append(head);
 card.append(el('div','label','用户输入'),el('div','message',t.user_message||'（空）'),el('div','label','Agent 回答'),el('div','message',t.assistant_answer||'（空）'));
 if(checkpoint){const cp=el('div','annotation');cp.append(el('strong','',`检查点：${t.human_checkpoint.question||''}`),el('div','muted',`阻塞原因：${t.human_checkpoint.blocking_reason||''}；需要：${t.human_checkpoint.requested_input||''}`));card.append(cp)}
 if(structured){const hi=el('div','annotation');hi.append(el('strong','',`人工回复：${t.human_intervention.response||''}`),el('div','muted',`归因：${t.human_intervention.attribution?.target||'unknown'} · ${t.human_intervention.attribution?.rationale||''}`));card.append(hi)}
 if(events.length){const box=el('div','tools'),d=el('details'),s=el('summary','',`查看本轮 ${events.length} 次工具调用`);d.append(s);events.forEach(x=>{const tool=el('div','tool'),h=el('div','tool-head');h.append(el('strong','',`${x.step??'-'} · ${x.tool_name||'unknown'}`),el('span',x.success?'ok':'bad',x.success?'成功':'失败'));const detail=el('details'),sum=el('summary','','参数与返回'),pre=el('div','mono',`参数\n${JSON.stringify(x.arguments||{},null,2)}\n\n返回\n${x.result||''}`);detail.append(sum,pre);tool.append(h,detail);d.append(tool)});box.append(d);card.append(box)}
 const note=el('div','annotation'),row=el('div','row'),mark=el('label'),check=document.createElement('input');check.type='checkbox';check.className='mark';check.checked=structured;mark.append(check,document.createTextNode(' 标记为有效人工纠偏'));const select=document.createElement('select');select.className='target';[['tool_selection','工具选择'],['skill_routing','Skill 路由'],['skill_content','Skill 内容'],['case_context','Case 输入'],['unknown','待人工归因']].forEach(([v,n])=>{const o=el('option','',n);o.value=v;select.append(o)});select.value=t.human_intervention?.attribution?.target||'unknown';const input=document.createElement('input');input.type='text';input.className='note';input.placeholder='说明这次提示纠正了什么';row.append(mark,select,input);note.append(row);card.append(note);root.append(card)});
function filter(){const q=document.getElementById('search').value.trim().toLowerCase(),to=document.getElementById('toolsOnly').checked,fo=document.getElementById('feedbackOnly').checked;document.querySelectorAll('.turn').forEach(c=>c.classList.toggle('hidden',!!((q&&!c.dataset.search.includes(q))||(to&&c.dataset.tools!=='1')||(fo&&c.dataset.feedback!=='1'))))}document.getElementById('search').oninput=filter;document.getElementById('toolsOnly').onchange=filter;document.getElementById('feedbackOnly').onchange=filter;
document.getElementById('export').onclick=()=>{const annotations=[...document.querySelectorAll('.turn')].map((c,i)=>{if(!c.querySelector('.mark').checked)return null;return{turn_index:turns[i].turn_index,target:c.querySelector('.target').value,note:c.querySelector('.note').value.trim(),user_message:turns[i].user_message,structured_intervention:!!turns[i].human_intervention}}).filter(Boolean);const out={schema_version:1,record_kind:'chat_review',session_id:session.session_id,source_updated_at:session.updated_at,annotations};const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`chat-review-${session.session_id||'session'}.json`;a.click();URL.revokeObjectURL(a.href)};
</script></body></html>'''
