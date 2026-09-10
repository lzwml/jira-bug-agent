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
    appendLinkedText(body, message.content || "");
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
      content: event.content || "", status: "completed",
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
