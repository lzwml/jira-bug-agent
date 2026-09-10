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
    evidence.append(element("summary", "", "查看证据定位（" + report.evidence.length + "）"));
    for (const item of report.evidence) {
      const button = element(
        "button", "location", item.evidence_id + " · " + item.relative_path +
        (item.line_start ? ":L" + item.line_start : ""),
      );
      button.onclick = () => vscode.postMessage({type: "openLocation", location: item});
      evidence.append(button);
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
      const button = element(
        "button", "location",
        (location.identifier ? location.identifier + " · " : "") +
        location.relative_path +
        (location.line_start ? ":L" + location.line_start : ""),
      );
      button.onclick = () => vscode.postMessage({type: "openLocation", location});
      list.append(button);
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
  for (const message of ordered) {
    const card = element("article", "message " + message.role);
    card.append(element("div", "label", message.role === "user" ? "你" : "BugAgent"));
    const body = element("div", "content");
    if (message.role === "assistant" && message.report) renderReport(body, message);
    else appendLinkedText(body, message.content || "");
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
