"""从 Run Bundle 生成无需服务端的本地执行复盘页。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from ..application.evaluation import _state_root, load_run_bundle
from ..infrastructure.persistence.runstore import _atomic_write


def render_run_visualization(run_path: Path, output_path: Path | None = None) -> Path:
    bundle = load_run_bundle(run_path)
    encoded = base64.b64encode(
        json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    if output_path is None:
        output_path = _state_root(run_path) / "visualizations" / f"{run_path.stem}.html"
    else:
        output_path = output_path.expanduser().resolve()
    html = _HTML.replace("__RUN_BUNDLE_BASE64__", encoded)
    _atomic_write(output_path, html)
    return output_path


_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>稳定性 Agent 执行复盘</title>
<style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#17202a;--muted:#667085;--line:#dfe4ea;--blue:#155eef;--green:#067647;--red:#b42318;--amber:#b54708}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}.top{padding:24px 30px;background:#101828;color:white}.top h1{margin:0 0 7px;font-size:24px}.sub{color:#cbd5e1}.layout{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;padding:18px;max-width:1600px;margin:auto}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px;box-shadow:0 1px 2px #1018280d}h2{font-size:17px;margin:0 0 13px}h3{font-size:14px;margin:0}.metrics{display:grid;grid-template-columns:repeat(5,minmax(100px,1fr));gap:10px}.metric{background:#f8fafc;border-radius:9px;padding:12px}.metric b{display:block;font-size:21px}.metric span,.muted{color:var(--muted)}.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#eaf0ff;color:#1849a9;font-size:12px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.step{border-left:3px solid var(--green);padding:3px 0 3px 13px;margin:15px 0}.step.fail{border-color:var(--red)}.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;background:#f8fafc;padding:10px;border-radius:7px}details{margin-top:7px}summary{cursor:pointer;color:var(--blue)}.claim,.evidence,.artifact{padding:11px 0;border-top:1px solid #edf0f3}.claim:first-child,.evidence:first-child,.artifact:first-child{border-top:0}.review{position:sticky;top:14px}.field{margin:11px 0}.field label{display:block;font-weight:600;margin-bottom:4px}input,select,textarea{width:100%;border:1px solid #cfd6df;border-radius:7px;padding:8px;background:white;color:var(--ink)}textarea{min-height:72px;resize:vertical}.score{display:grid;grid-template-columns:1fr 48px;gap:7px;align-items:center}.score input{padding:0}.buttons{display:flex;gap:8px;margin-top:14px}button{border:0;border-radius:8px;padding:10px 14px;background:var(--blue);color:white;font-weight:650;cursor:pointer}button.secondary{background:#eef2f6;color:#344054}.hint{font-size:12px;color:var(--muted)}@media(max-width:980px){.layout{grid-template-columns:1fr}.review{position:static}.metrics{grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><header class="top"><h1>稳定性 Agent 执行复盘</h1><div class="sub" id="subtitle"></div></header>
<main class="layout"><section>
<div class="card"><h2>运行概览</h2><div class="metrics" id="metrics"></div></div>
<div class="card"><h2>结论与主张</h2><div id="claims"></div></div>
<div class="card"><h2>证据注册表</h2><div id="evidence"></div></div>
<div class="card"><h2>执行过程</h2><div id="timeline"></div></div>
<div class="card"><h2>本轮实际输入</h2><div id="inputs"></div></div>
<div class="card"><h2>复现身份</h2><div id="provenance" class="mono"></div></div>
</section><aside><form class="card review" id="review"><h2>人工复核反馈</h2>
<div class="field"><label>复核人</label><input id="reviewer" required placeholder="姓名或团队"></div>
<div class="field"><label>复核结果</label><select id="verdict"><option value="accepted">结论可接受</option><option value="corrected">需要纠正</option><option value="rejected">拒绝作为黄金 Case</option></select></div>
<div id="scores"></div><div class="field"><label>标签（逗号分隔）</label><input id="labels" placeholder="watchdog, system_server"></div>
<div class="field"><label>复核说明</label><textarea id="notes" placeholder="指出正确点、错误点和遗漏"></textarea></div>
<details id="correction"><summary>填写纠正后的金标准</summary>
<div class="field"><label>结论等级</label><select id="goldStatus"><option value="">沿用原结论</option><option>confirmed</option><option>hypothesis_only</option><option>insufficient_evidence</option></select></div>
<div class="field"><label>正确根因</label><textarea id="rootCause"></textarea></div>
<div class="field"><label>根因 Evidence ID（逗号分隔）</label><input id="evidenceIds"></div>
<div class="field"><label>必须包含的主张（每行一条）</label><textarea id="requiredClaims"></textarea></div>
<div class="field"><label>禁止出现的错误主张（每行一条）</label><textarea id="forbiddenClaims"></textarea></div>
<div class="field"><label>必须明确的缺失证据（每行一条）</label><textarea id="missingEvidence"></textarea></div></details>
<p class="hint">导出只生成本地 JSON，不会修改原始 Run Bundle。之后用 eval-review 导入并晋升。</p>
<div class="buttons"><button type="submit">导出反馈 JSON</button><button type="button" class="secondary" id="fill">沿用当前结论</button></div>
</form></aside></main>
<script>
const bundle=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('__RUN_BUNDLE_BASE64__'),c=>c.charCodeAt(0))));
const esc=v=>String(v??'');const pretty=v=>JSON.stringify(v,null,2);const lines=id=>document.getElementById(id).value.split('\n').map(x=>x.trim()).filter(Boolean);const csv=id=>document.getElementById(id).value.split(',').map(x=>x.trim()).filter(Boolean);
const result=bundle.result||{}, report=result.report||{}, derived=bundle.derived||{}, budget=bundle.budget||{}, actual=budget.actual||{}, provenance=bundle.provenance||{}, fp=derived.input_fingerprint||{}, agentMetrics=derived.agent_metrics||{};
document.getElementById('subtitle').textContent=`${bundle.task?.task_id||''} · ${bundle.task?.objective||''}`;
const tokenTotal=actual.token_usage==null?'未知':String(actual.token_usage.total_tokens)+(actual.token_usage.complete===false?'（部分）':'');const metricData=[['状态',result.status||bundle.lifecycle?.status],['分析工具',agentMetrics.analysis_tool_calls??actual.tool_calls??0],['Token',tokenTotal],['人工检查点',agentMetrics.human_checkpoint_count??0],['重复调用',agentMetrics.repeated_identical_calls??0],['证据',derived.evidence_registry?.length??0],['耗时',actual.duration_ms==null?'未知':actual.duration_ms+' ms']];
const metrics=document.getElementById('metrics');metricData.forEach(([k,v])=>{const x=document.createElement('div');x.className='metric';const b=document.createElement('b');b.textContent=esc(v);const s=document.createElement('span');s.textContent=k;x.append(b,s);metrics.append(x)});
const claims=document.getElementById('claims');if(result.human_checkpoint){const h=document.createElement('div');h.className='claim warn';h.textContent=`等待人工提示：${result.human_checkpoint.question}；需要：${result.human_checkpoint.requested_input}`;claims.append(h)}const status=document.createElement('p');status.innerHTML=`<span class="badge"></span>`;status.querySelector('span').textContent=report.conclusion_status||'unknown';claims.append(status);(derived.claim_snapshot?.claims||[]).forEach(c=>{const x=document.createElement('div');x.className='claim';const h=document.createElement('h3');h.textContent=`${c.kind}${c.status?' · '+c.status:''}`;const p=document.createElement('div');p.textContent=c.statement;const ids=document.createElement('div');ids.className='hint';ids.textContent=[...(c.evidence_ids||[]),...(c.supporting_evidence_ids||[])].join(', ');x.append(h,p,ids);claims.append(x)});if(!(derived.claim_snapshot?.claims||[]).length)claims.append(Object.assign(document.createElement('p'),{textContent:'本轮没有结构化主张。',className:'muted'}));
const ev=document.getElementById('evidence');(derived.evidence_registry||[]).forEach(e=>{const x=document.createElement('div');x.className='evidence';const h=document.createElement('h3');h.textContent=e.evidence_id;const p=document.createElement('div');p.textContent=`${e.relative_path}${e.line_start?' · L'+e.line_start:''}`;const q=document.createElement('div');q.className='mono';q.textContent=e.excerpt||'（无摘录）';x.append(h,p,q);ev.append(x)});if(!(derived.evidence_registry||[]).length)ev.textContent='本轮没有注册到可复核 Evidence。';
const timeline=document.getElementById('timeline');(bundle.trace||[]).forEach(t=>{const x=document.createElement('div');x.className='step'+(t.success?'':' fail');const head=document.createElement('div');head.className='row';const h=document.createElement('h3');h.textContent=`第 ${t.step} 步 · ${t.tool_name}`;const state=document.createElement('span');state.className=t.success?'ok':'bad';state.textContent=t.success?'成功':'失败';head.append(h,state);const d=document.createElement('details');const sm=document.createElement('summary');sm.textContent='查看参数与返回';const pre=document.createElement('div');pre.className='mono';pre.textContent='参数\n'+pretty(t.arguments)+'\n\n返回\n'+t.result;d.append(sm,pre);x.append(head,d);timeline.append(x)});if(!(bundle.trace||[]).length)timeline.textContent='Agent 尚未产生工具调用。';
const inputs=document.getElementById('inputs');const cov=document.createElement('p');cov.className=fp.complete_content_fingerprint?'ok':'warn';cov.textContent=fp.complete_content_fingerprint?'输入已有完整内容指纹':'输入内容哈希覆盖不完整，回归时需谨慎解释';inputs.append(cov);(fp.artifact_manifest||[]).forEach(a=>{const x=document.createElement('div');x.className='artifact';const h=document.createElement('h3');h.textContent=a.relative_path||a.artifact_id;const p=document.createElement('div');p.className='hint';p.textContent=`${a.kind||'unknown'} · ${a.size_bytes??'?'} bytes · ${a.content_sha256?'内容已哈希':'仅元数据指纹'}`;x.append(h,p);inputs.append(x)});
document.getElementById('provenance').textContent=pretty({model:provenance.model,package:provenance.package,build_revision:provenance.build_revision,build_revision_authoritative:provenance.build_revision_authoritative,prompts:provenance.prompts,tool_schema_sha256:provenance.tool_schema_sha256,skills:provenance.skills,budgets:provenance.budgets});
const dims=[['incident_identity','事故身份'],['evidence_grounding','证据落地'],['causal_correctness','因果正确性'],['android_stability_coverage','Android 稳定性覆盖'],['actionability','可执行性']];const scoreBox=document.getElementById('scores');dims.forEach(([id,label])=>{const f=document.createElement('div');f.className='field';const l=document.createElement('label');l.textContent=label+'（0–4）';const row=document.createElement('div');row.className='score';const input=document.createElement('input');input.type='range';input.min=0;input.max=4;input.value=3;input.id=id;const out=document.createElement('input');out.value=3;out.readOnly=true;input.oninput=()=>out.value=input.value;row.append(input,out);f.append(l,row);scoreBox.append(f)});
document.getElementById('fill').onclick=()=>{document.getElementById('goldStatus').value=report.conclusion_status||'';document.getElementById('rootCause').value=report.root_cause||'';document.getElementById('evidenceIds').value=(report.root_cause_evidence_ids||[]).join(', ');document.getElementById('requiredClaims').value=(derived.claim_snapshot?.claims||[]).map(x=>x.statement).filter(Boolean).join('\n');};
document.getElementById('review').onsubmit=e=>{e.preventDefault();const verdict=document.getElementById('verdict').value;const data={reviewer:document.getElementById('reviewer').value.trim(),verdict,scores:Object.fromEntries(dims.map(([id])=>[id,Number(document.getElementById(id).value)])),labels:csv('labels'),notes:document.getElementById('notes').value};if(!data.reviewer){alert('请填写复核人');return}if(verdict==='corrected'){data.expectation={};const gs=document.getElementById('goldStatus').value;if(gs)data.expectation.conclusion_status=gs;data.expectation.root_cause=document.getElementById('rootCause').value.trim()||null;data.expectation.evidence_ids=csv('evidenceIds');data.expectation.required_claims=lines('requiredClaims');data.expectation.forbidden_claims=lines('forbiddenClaims');data.expectation.required_missing_evidence=lines('missingEvidence')};const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`review-${bundle.task?.task_id||'run'}.json`;a.click();URL.revokeObjectURL(a.href)};
</script></body></html>'''
