"use strict";

const vscode = require("vscode");
const crypto = require("crypto");
const path = require("path");
const {ServerManager, SECRET_KEY} = require("./server");
const {LargeLogProvider, LogOpener} = require("./logs");
const {ConversationPanel} = require("./panel");
const {EvidenceAnnotationManager} = require("./annotations");

async function activate(context) {
  const output = vscode.window.createOutputChannel("BugAgent");
  const server = new ServerManager(context, output);
  const largeLogs = new LargeLogProvider();
  const logOpener = new LogOpener(largeLogs);
  const annotations = new EvidenceAnnotationManager(context, largeLogs);
  const panels = new ConversationPanel(
    context, server, logOpener, annotations, () => panels.refreshHistory(),
  );

  context.subscriptions.push(
    output,
    vscode.workspace.registerTextDocumentContentProvider("bugagent-log", largeLogs),
    vscode.window.registerWebviewViewProvider("bugAgent.chat", panels, {
      webviewOptions: {retainContextWhenHidden: true},
    }),
    vscode.commands.registerCommand("bugAgent.refresh", () => panels.refreshHistory()),
    vscode.commands.registerCommand(
      "bugAgent.toggleEvidenceAnnotations", () => annotations.toggle(),
    ),
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
    vscode.commands.registerCommand("bugAgent.newLocalCase", async (options = {}) => {
      let casePath = options.casePath;
      if (!casePath) {
        const selected = await vscode.window.showOpenDialog({
          canSelectFiles: false,
          canSelectFolders: true,
          canSelectMany: false,
          title: "选择本地 Bug Case",
        });
        if (!selected || !selected[0]) return;
        casePath = selected[0].fsPath;
      }
      const objective = options.objective || "定位 Bug 根因并给出下一步建议";
      await createAndOpen(
        server, panels,
        {source: "local", case_path: casePath, objective},
        casePath, Boolean(options.sendImmediately),
      );
    }),
    vscode.commands.registerCommand("bugAgent.newJiraCase", async (options = {}) => {
      const issueKey = options.issueKey || await vscode.window.showInputBox({
          prompt: "Jira Issue Key",
          placeHolder: "APP-42",
          validateInput: (value) =>
            /^[A-Za-z][A-Za-z0-9_]*-\d+$/.test(value) ? undefined : "请输入有效的 Issue Key",
        });
      if (!issueKey) return;
      const objective = options.objective || "定位 Bug 根因并给出下一步建议";
      await createAndOpen(
        server, panels,
        {source: "jira", issue_key: issueKey.toUpperCase(), objective},
        undefined, Boolean(options.sendImmediately),
      );
    }),
    {dispose: () => server.stop()},
  );
}

async function createAndOpen(server, panels, taskFields, caseRoot, sendImmediately = false) {
  try {
    await vscode.window.withProgress({
      location: vscode.ProgressLocation.Notification,
      title: "BugAgent 正在启动分析…",
    }, async () => {
      const api = await server.ensure(caseRoot);
      const conversations = await api.listConversations();
      const existing = conversations.find((item) =>
        item.status === "active" && sameCase(item.task || {}, taskFields));
      if (existing) {
        if (sendImmediately) {
          await api.sendMessage(existing.conversation_id, taskFields.objective);
        }
        const record = await api.getConversation(existing.conversation_id);
        await panels.refreshHistory();
        await panels.open(record);
        return;
      }
      const created = await api.createConversation({
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
      await panels.refreshHistory();
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
