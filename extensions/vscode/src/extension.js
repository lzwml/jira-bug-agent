"use strict";

const vscode = require("vscode");
const crypto = require("crypto");
const path = require("path");
const {ServerManager, SECRET_KEY} = require("./server");
const {LargeLogProvider, LogOpener} = require("./logs");
const {ConversationPanel} = require("./panel");

class ConversationTree {
  constructor(server) {
    this.server = server;
    this.changed = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.changed.event;
  }

  refresh() { this.changed.fire(); }

  async getChildren() {
    try {
      const api = await this.server.ensure();
      return await api.listConversations();
    } catch (error) {
      return [{error: error.message}];
    }
  }

  getTreeItem(item) {
    if (item.error) {
      const treeItem = new vscode.TreeItem("服务未连接");
      treeItem.description = item.error;
      treeItem.iconPath = new vscode.ThemeIcon("warning");
      return treeItem;
    }
    const task = item.task || {};
    const label = task.issue_key || path.basename(task.case_path || item.conversation_id);
    const treeItem = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
    treeItem.description = item.status;
    treeItem.tooltip = task.objective || item.conversation_id;
    treeItem.iconPath = new vscode.ThemeIcon(
      item.messages && item.messages.some((message) =>
        message.status === "queued" || message.status === "running")
        ? "loading~spin" : "comment-discussion",
    );
    treeItem.command = {
      command: "bugAgent.openConversation",
      title: "打开 BugAgent 会话",
      arguments: [item],
    };
    return treeItem;
  }
}

async function activate(context) {
  const output = vscode.window.createOutputChannel("BugAgent");
  const server = new ServerManager(context, output);
  const largeLogs = new LargeLogProvider();
  const logOpener = new LogOpener(largeLogs);
  const tree = new ConversationTree(server);
  const panels = new ConversationPanel(context, server, logOpener, () => tree.refresh());

  context.subscriptions.push(
    output,
    vscode.workspace.registerTextDocumentContentProvider("bugagent-log", largeLogs),
    vscode.window.registerTreeDataProvider("bugAgent.conversations", tree),
    vscode.commands.registerCommand("bugAgent.refresh", () => tree.refresh()),
    vscode.commands.registerCommand("bugAgent.openConversation", (record) => panels.open(record)),
    vscode.commands.registerCommand("bugAgent.setApiKey", async () => {
      const value = await vscode.window.showInputBox({
        prompt: "BugAgent API Key",
        password: true,
        ignoreFocusOut: true,
      });
      if (value !== undefined) {
        await context.secrets.store(SECRET_KEY, value);
        vscode.window.showInformationMessage("BugAgent API Key stored in VS Code Secret Storage.");
      }
    }),
    vscode.commands.registerCommand("bugAgent.startServer", async () => {
      try {
        await server.start();
        vscode.window.showInformationMessage("BugAgent 服务正在启动。");
      } catch (error) {
        vscode.window.showErrorMessage("BugAgent: " + error.message);
      }
    }),
    vscode.commands.registerCommand("bugAgent.stopServer", () => server.stop()),
    vscode.commands.registerCommand("bugAgent.newLocalCase", async () => {
      const selected = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        title: "选择本地 Bug Case",
      });
      if (!selected || !selected[0]) return;
      const objective = await vscode.window.showInputBox({
        prompt: "分析目标",
        value: "定位 Bug 根因并给出下一步建议",
        ignoreFocusOut: true,
      });
      if (!objective) return;
      await createAndOpen(
        server, panels, tree,
        {source: "local", case_path: selected[0].fsPath, objective},
        selected[0].fsPath,
      );
    }),
    vscode.commands.registerCommand("bugAgent.newJiraCase", async () => {
      const issueKey = await vscode.window.showInputBox({
        prompt: "Jira Issue Key",
        placeHolder: "APP-42",
        validateInput: (value) =>
          /^[A-Za-z][A-Za-z0-9_]*-\d+$/.test(value) ? undefined : "请输入有效的 Issue Key",
      });
      if (!issueKey) return;
      const objective = await vscode.window.showInputBox({
        prompt: "分析目标",
        value: "定位 Bug 根因并给出下一步建议",
      });
      if (!objective) return;
      await createAndOpen(
        server, panels, tree,
        {source: "jira", issue_key: issueKey.toUpperCase(), objective},
      );
    }),
    {dispose: () => server.stop()},
  );
}

async function createAndOpen(server, panels, tree, taskFields, caseRoot) {
  try {
    await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification,
      title: "BugAgent 正在启动分析…",
    }, async () => {
      const api = await server.ensure(caseRoot);
      const conversations = await api.listConversations();
      const existing = conversations.find((item) =>
        item.status === "active" && sameCase(item.task || {}, taskFields));
      let forceNew = false;
      if (existing) {
        const choice = await vscode.window.showQuickPick([
          {
            label: "$(history) 继续已有会话",
            description: "推荐·保留已查证据、人工纠偏和调查上下文",
            resume: true,
          },
          {
            label: "$(new-file) 重新分析",
            description: "创建独立新会话，不继承上一次对话",
            resume: false,
          },
        ], {
          placeHolder: "已找到该 Case 的历史会话",
          ignoreFocusOut: true,
        });
        if (!choice) return;
        if (choice.resume) {
          const record = await api.getConversation(existing.conversation_id);
          tree.refresh();
          await panels.open(record);
          vscode.window.showInformationMessage("已恢复该 Case 的历史会话。");
          return;
        }
        forceNew = true;
      }
      const created = await api.createConversation({
        ...(forceNew ? {conversation_id: "vscode-" + crypto.randomUUID()} : {}),
        task: {
          task_id: "chat-" + crypto.randomUUID(),
          include_trace: true,
          goal_mode: vscode.workspace.getConfiguration("bugAgent").get("goalMode", true),
          ...taskFields,
        },
      });
      if (!(created.messages || []).length) {
        await api.sendMessage(
          created.conversation_id,
          "开始分析这个 Case。目标：" + taskFields.objective,
        );
      }
      const record = await api.getConversation(created.conversation_id);
      tree.refresh();
      await panels.open(record);
    });
  } catch (error) {
    vscode.window.showErrorMessage("BugAgent: " + error.message);
  }
}

function sameCase(left, right) {
  if (left.source !== right.source) return false;
  if (left.source === "jira") {
    return String(left.issue_key || "").toUpperCase() ===
      String(right.issue_key || "").toUpperCase();
  }
  const normalize = (value) => path.resolve(String(value || "")).toLowerCase();
  return Boolean(left.case_path && right.case_path) &&
    normalize(left.case_path) === normalize(right.case_path);
}

function deactivate() {}

module.exports = {activate, deactivate};
