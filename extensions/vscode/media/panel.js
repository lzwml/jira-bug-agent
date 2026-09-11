"use strict";
const vscode = acquireVsCodeApi();
const root = document.getElementById("messages");
const input = document.getElementById("input");
const send = document.getElementById("send");
const cancel = document.getElementById("cancel");
const status = document.getElementById("status");
const messages = new Map();
const tools = new Map();
let conversation;
let activeMessageId;
const markdown = globalThis.markdownit({
  html: false,
  linkify: true,
  breaks: false,
  typographer: false,
});

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function pathParts(raw) {
  let value = raw;
  const line = value.match(/:(\d+)(?:-(\d+))?$/);
  if (!line) return {relative_path: value};
  value = value.slice(0, line.index);
  return {
    relative_path: value,
    line_start: Number(line[1]),
    line_end: Number(line[2] || line[1]),
  };
}

function appendLinkedText(container, text) {
  const pattern = /[A-Za-z]:\\[^\s"'<>|]+(?::\d+(?:-\d+)?)?/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    container.append(document.createTextNode(text.slice(offset, match.index)));
    const button = element("button", "path", match[0]);
    button.onclick = () => vscode.postMessage({
      type: "openLocation", location: pathParts(match[0]),
    });
    container.append(button);
    offset = match.index + match[0].length;
  }
  container.append(document.createTextNode(text.slice(offset)));
}

function appendMarkdown(container, text) {
  const rendered = element("div", "markdown-body");
  rendered.innerHTML = markdown.render(String(text || ""));
  for (const link of rendered.querySelectorAll("a[href]")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      vscode.postMessage({type: "openExternal", href: link.href});
    });
  }
  container.append(rendered);
}

const statusLabels = {
  confirmed: "根因已确认",
  hypothesis_only: "已形成候选根因",
  insufficient_evidence: "根因未确认（证据不足）",
};
const hypothesisStatusLabels = {
  candidate: "待验证",
  supported: "有证据支持",
  rejected: "已排除",
};

function reportField(container, label, value, className) {
  const row = element("div", "report-field" + (className ? " " + className : ""));
  row.append(
    element("span", "report-key", label),
    element("span", "report-value", value || "尚未确认"),
  );
  container.append(row);
}

function reportList(container, title, values, className) {
  if (!values || !values.length) return;
  const section = element("section", "report-section" + (className ? " " + className : ""));
  section.append(element("h3", "", title));
  const list = element("ul");
  for (const value of values) list.append(element("li", "", value));
  section.append(list);
  container.append(section);
}

function basename(rawPath) {
  const parts = String(rawPath || "").replaceAll("\\", "/").split("!/");
  return (parts.at(-1) || "文件").split("/").at(-1);
}

function locationForEvidence(message, evidence) {
  const location = (message.locations || []).find((item) =>
    item.identifier === evidence.evidence_id);
  return {...evidence, ...(location || {})};
}

function renderFileCard(location, relation, annotationMessage) {
  const button = element("button", "evidence-file");
  const isArchive = Boolean(location.archive_relative_path ||
    String(location.relative_path || "").includes("!/"));
  const icon = element("span", "evidence-file-icon", isArchive ? "▣" : "▤");
  const body = element("span", "evidence-file-body");
  const title = element("span", "evidence-file-title");
  title.append(
    element("span", "evidence-file-name", basename(location.relative_path)),
    element(
      "span",
      "evidence-file-state " + (location.availability || "missing"),
      location.availability === "ready" ? "可打开" :
        location.availability === "not_extracted" ? "未提取" : "待定位",
    ),
  );
  const anchors = [];
  if (location.line_start) {
    anchors.push("L" + location.line_start +
      (location.line_end && location.line_end !== location.line_start
        ? "–" + location.line_end : ""));
  } else if (location.timestamp_ms !== undefined && location.timestamp_ms !== null) {
    anchors.push(String(location.timestamp_ms) + " ms");
  }
  if (relation) anchors.push(relation);
  body.append(title);
  if (anchors.length) body.append(element("span", "evidence-file-meta", anchors.join(" · ")));
  body.append(element("span", "evidence-file-path", location.relative_path || "路径未记录"));
  if (location.excerpt) body.append(element("span", "evidence-file-excerpt", location.excerpt));
  button.append(icon, body, element("span", "evidence-file-open", "在右侧打开 ›"));
  button.onclick = () => vscode.postMessage({
    type: "openLocation", location, annotationMessage,
  });
  return button;
}

function renderEvidenceCards(container, ids, report, message, title, relation) {
  const wanted = new Set(ids || []);
  const evidence = (report.evidence || []).filter((item) => wanted.has(item.evidence_id));
  if (!evidence.length) return;
  const group = element("div", "evidence-files");
  if (title) group.append(element("div", "evidence-files-label", title));
  for (const item of evidence) {
    group.append(renderFileCard(
      locationForEvidence(message, item), relation,
      {message_id: message.message_id, report, locations: message.locations || []},
    ));
  }
  container.append(group);
}

function turnLocationsForAssistant(ordered, index) {
  let userMessage;
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    if (ordered[cursor].role === "user") {
      userMessage = ordered[cursor];
      break;
    }
  }
  if (!userMessage) return [];
  const unique = new Map();
  for (const tool of tools.values()) {
    if (tool.message_id !== userMessage.message_id) continue;
    for (const location of tool.locations || []) {
      const key = [location.identifier, location.relative_path, location.line_start].join("|");
      unique.set(key, location);
    }
  }
  return [...unique.values()];
}

function renderTurnFiles(container, locations, report) {
  const reportIds = new Set((report && report.evidence || []).map((item) => item.evidence_id));
  const relevant = locations.filter((location) => {
    if (reportIds.has(location.identifier)) return false;
    const identifier = String(location.identifier || "").toLowerCase();
    return Boolean(location.line_start) || identifier.startsWith("ev-") ||
      identifier.startsWith("evidence_") || identifier.startsWith("finding");
  });
  if (!relevant.length) return;
  const group = element("div", "evidence-files turn-evidence-files");
  group.append(element("div", "evidence-files-label", "本轮相关文件"));
  for (const location of relevant.slice(0, 5)) {
    group.append(renderFileCard(location, "工具已定位"));
  }
  if (relevant.length > 5) {
    const more = element("details", "evidence-files-more");
    more.append(element("summary", "", "另外 " + String(relevant.length - 5) + " 个文件"));
    for (const location of relevant.slice(5)) {
      more.append(renderFileCard(location, "工具已定位"));
    }
    group.append(more);
  }
  container.append(group);
}

function renderPersistence(container, persistence) {
  if (!persistence) return;
  const statusRow = element("div", "persistence-status");
  statusRow.append(element("span", "validation grounded", "会话已保存"));
  if (persistence.investigation_state_saved) {
    statusRow.append(element("span", "validation grounded", "调查状态已保存"));
  }
  if (persistence.rca_saved) {
    statusRow.append(element("span", "validation grounded", "RCA 已保存"));
  }
  container.append(statusRow);
  const paths = [
    ["可读报告", persistence.rca_markdown_path],
    ["结构化状态", persistence.rca_state_path],
    ["变更记录", persistence.rca_events_path],
  ].filter((item) => item[1]);
  if (!paths.length) return;
  const details = element("details", "persistence-paths");
  details.append(element("summary", "", "查看本地保存位置"));
  for (const [label, path] of paths) {
    const row = element("div", "persistence-path");
    row.append(element("span", "persistence-label", label + "："));
    appendLinkedText(row, path);
    details.append(row);
  }
  container.append(details);
}

function renderReport(container, message) {
  const report = message.report;
  const head = element("div", "report-head");
  head.append(element(
    "span", "report-status " + report.conclusion_status,
    statusLabels[report.conclusion_status] || report.conclusion_status,
  ));
  if (message.report_validation) {
    head.append(element(
      "span",
      message.report_validation.grounded ? "validation grounded" : "validation ungrounded",
      message.report_validation.grounded ? "引用校验通过" : "引用校验未通过",
    ));
  }
  container.append(head);
  if (report.conclusion_status !== "confirmed") {
    container.append(element(
      "div", "report-warning",
      "当前没有已验证的技术根因；下方因果解释均是候选假设，不能作为定案结论。",
    ));
  }
  const facts = element("div", "report-facts");
  reportField(facts, "用户现象", report.observed_symptom);
  reportField(facts, "直接机制", report.failure_mechanism);
  reportField(facts, "技术根因", report.root_cause, report.root_cause ? "" : "unconfirmed");
  container.append(facts);
  renderEvidenceCards(
    container, report.root_cause_evidence_ids, report, message,
    "支撑技术根因的文件", "根因证据",
  );
  const summary = element("section", "report-section report-summary");
  const summaryText = report.conclusion_status !== "confirmed" &&
    !(report.summary || "").includes("根因尚未确认")
    ? "根因尚未确认。当前证据下：" + (report.summary || "暂无摘要")
    : (report.summary || "暂无摘要");
  summary.append(
    element("h3", "", "当前结论"),
    element("p", "", summaryText),
  );
  container.append(summary);
  reportList(container, "已确认事实", report.confirmed_facts);

  if (report.hypotheses && report.hypotheses.length) {
    const section = element("section", "report-section hypotheses");
    section.append(element("h3", "", "候选假设与验证缺口"));
    for (const hypothesis of report.hypotheses) {
      const item = element("div", "hypothesis");
      item.append(element("strong", "", hypothesis.statement));
      item.append(element(
        "div", "muted", "状态：" +
        (hypothesisStatusLabels[hypothesis.status] || hypothesis.status) + " · 置信度：" +
        Math.round(Number(hypothesis.confidence || 0) * 100) + "%",
      ));
      if (hypothesis.missing_evidence && hypothesis.missing_evidence.length) {
        item.append(element("div", "gap", "尚缺：" + hypothesis.missing_evidence.join("；")));
      }
      if (hypothesis.falsification) {
        item.append(element("div", "", "最低成本验证：" + hypothesis.falsification));
      }
      renderEvidenceCards(
        item, hypothesis.supporting_evidence_ids, report, message,
        "支持该假设", "支持证据",
      );
      renderEvidenceCards(
        item, hypothesis.contradicting_evidence_ids, report, message,
        "反驳该假设", "反证",
      );
      section.append(item);
    }
    container.append(section);
  }
  reportList(container, "整体缺失证据", report.missing_evidence, "gaps");
  const actions = (report.actions || []).map((item) =>
    item.priority + " · " + item.action + "（完成标准：" + item.completion_criteria + "）");
  reportList(container, "下一步动作", actions.length ? actions : report.next_actions);

  if (report.evidence && report.evidence.length) {
    const evidence = element("details", "report-evidence");
    evidence.append(element("summary", "", "全部证据文件（" + report.evidence.length + "）"));
    for (const item of report.evidence) {
      evidence.append(renderFileCard(locationForEvidence(message, item), "证据 " + item.evidence_id));
    }
    container.append(evidence);
  }
}

function renderTool(event) {
  const card = element("details", "tool");
  card.append(element(
    "summary", event.success ? "success" : "failure",
    "第 " + String(event.step || "?") + " 步 · " + (event.tool_name || "工具"),
  ));
    const body = element("div", "tool-body");
  body.append(element("pre", "", JSON.stringify(event.arguments || {}, null, 2)));
  const locations = event.locations || [];
  if (locations.length) {
    const list = element("div", "locations");
    for (const location of locations) {
      list.append(renderFileCard(
        location,
        location.identifier ? "定位 " + location.identifier : "工具返回",
      ));
    }
    body.append(list);
  }
  const raw = element("details", "raw");
  raw.append(element("summary", "", "查看工具返回"));
  raw.append(element("pre", "", event.result || ""));
  body.append(raw);
  card.append(body);
  return card;
}

function render() {
  root.replaceChildren();
  const ordered = [...messages.values()].sort((a, b) => a.message_id - b.message_id);
  for (let index = 0; index < ordered.length; index += 1) {
    const message = ordered[index];
    const card = element("article", "message " + message.role);
    card.append(element("div", "label", message.role === "user" ? "你" : "BugAgent"));
    const body = element("div", "content");
    if (message.role === "assistant" && message.report) renderReport(body, message);
    else if (message.role === "assistant") appendMarkdown(body, message.content || "");
    else appendLinkedText(body, message.content || "");
    if (message.role === "assistant") {
      renderTurnFiles(body, turnLocationsForAssistant(ordered, index), message.report);
    }
    if (message.role === "assistant") renderPersistence(body, message.persistence);
    card.append(body);
    if (message.status && message.status !== "completed") {
      card.append(element("div", "message-status", message.status));
    }
    root.append(card);
    for (const tool of [...tools.values()]
      .filter((item) => item.message_id === message.message_id)
      .sort((a, b) => (a.step || 0) - (b.step || 0))) {
      root.append(renderTool(tool));
    }
  }
  root.scrollTop = root.scrollHeight;
}

function setBusy(busy) {
  send.disabled = busy;
  cancel.disabled = !busy;
  status.textContent = busy ? "Agent 正在分析…" : "就绪";
}

function applyConversation(record) {
  conversation = record;
  document.getElementById("title").textContent =
    record.task.issue_key || record.task.case_path || "BugAgent";
  messages.clear();
  for (const message of record.messages || []) messages.set(message.message_id, message);
  const active = [...messages.values()].find((message) =>
    message.role === "user" &&
    (message.status === "queued" || message.status === "running"));
  activeMessageId = active ? active.message_id : undefined;
  setBusy(Boolean(activeMessageId));
  render();
}

function applyEvent(event) {
  if (event.kind === "message_queued") {
    messages.set(event.message_id, {
      message_id: event.message_id, role: "user",
      content: event.content || "", status: "queued",
    });
    activeMessageId = event.message_id;
    setBusy(true);
  } else if (event.kind === "message_running") {
    const message = messages.get(event.message_id);
    if (message) message.status = "running";
    status.textContent = "正在准备 Case 与调查上下文…";
  } else if (event.kind === "phase_changed") {
    status.textContent = "第 " + String(event.step || "?") + " 步 · " +
      (event.content || "正在分析");
  } else if (event.kind === "tool_started") {
    status.textContent = "第 " + String(event.step || "?") + " 步 · 正在执行 " +
      (event.tool_name || "工具");
  } else if (event.kind === "tool_completed") {
    tools.set(event.event_id, event);
    status.textContent = "第 " + String(event.step || "?") + " 步 · " +
      (event.tool_name || "工具") + " 已完成，正在检查结果…";
  } else if (event.kind === "assistant_message") {
    messages.set(event.message_id, {
      message_id: event.message_id, role: "assistant",
      content: event.content || "", content_format: event.content_format || "plain_text",
      report: event.report || null,
      report_validation: event.report_validation || null,
      locations: event.locations || [],
      persistence: event.persistence || null,
      status: "completed",
    });
    const active = messages.get(activeMessageId);
    if (active) active.status = "completed";
    activeMessageId = undefined;
    setBusy(false);
  } else if (event.kind === "turn_failed" || event.kind === "turn_cancelled") {
    const active = messages.get(event.message_id);
    if (active) active.status = event.kind === "turn_failed" ? "failed" : "cancelled";
    activeMessageId = undefined;
    setBusy(false);
  }
  render();
}

send.onclick = () => {
  const content = input.value.trim();
  if (!content || !conversation) return;
  vscode.postMessage({type: "send", content});
  input.value = "";
};
cancel.onclick = () => {
  if (activeMessageId) vscode.postMessage({type: "cancel", messageId: activeMessageId});
};
document.getElementById("refresh").onclick = () => vscode.postMessage({type: "refresh"});
input.onkeydown = (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) send.click();
};
window.addEventListener("message", ({data}) => {
  if (data.type === "conversation") applyConversation(data.value);
  else if (data.type === "event") applyEvent(data.value);
  else if (data.type === "pending") {
    messages.set(data.value.message_id, data.value);
    activeMessageId = data.value.message_id;
    setBusy(true);
    render();
  } else if (data.type === "connection" || data.type === "error") {
    status.textContent = data.value;
  }
});
