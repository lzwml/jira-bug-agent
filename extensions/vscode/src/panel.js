"use strict";

const vscode = require("vscode");
const path = require("path");
const fs = require("fs");

class ConversationPanel {
  constructor(context, server, logOpener, annotations, onChanged) {
    this.context = context;
    this.server = server;
    this.logOpener = logOpener;
    this.annotations = annotations;
    this.onChanged = onChanged;
    this.view = undefined;
    this.record = undefined;
    this.abort = undefined;
    this.cursor = 0;
    this.messageSubscription = undefined;
    this.lastError = "";
  }

  async resolveWebviewView(view) {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    const markdownRoot = vscode.Uri.joinPath(
      this.context.extensionUri, "node_modules", "markdown-it", "dist",
    );
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [media, markdownRoot],
    };
    view.webview.html = this.html(view.webview, media);
    this.messageSubscription?.dispose();
    this.messageSubscription = view.webview.onDidReceiveMessage(
      (message) => this.handle(message),
    );
    view.onDidDispose(() => {
      this.abort?.abort();
      this.messageSubscription?.dispose();
      this.messageSubscription = undefined;
      this.view = undefined;
    });
    await this.refreshHistory();
    if (this.record) {
      this.post({type: "conversation", value: this.record});
      this.stream();
    } else {
      this.post({type: "empty"});
    }
  }

  async open(record) {
    await this.annotations.syncRecord(record);
    this.abort?.abort();
    this.record = record;
    this.cursor = 0;
    await vscode.commands.executeCommand("bugAgent.chat.focus");
    if (!this.view) return;
    this.view.title = this.title(record);
    this.post({type: "conversation", value: record});
    await this.refreshHistory();
    this.stream();
  }

  hiddenConversationIds() {
    return new Set(this.context.globalState.get("bugAgent.hiddenConversations", []));
  }

  async refreshHistory() {
    try {
      const api = await this.server.ensure(
        this.record?.case_root || this.record?.task?.case_path,
      );
      const hidden = this.hiddenConversationIds();
      const records = (await api.listConversations())
        .filter((item) => !hidden.has(item.conversation_id));
      this.post({type: "history", value: records});
      this.post({type: "connection", value: "已连接"});
      this.lastError = "";
    } catch (error) {
      this.lastError = error.message;
      this.post({type: "connectionError", value: error.message});
    }
  }

  title(record) {
    const task = record.task || {};
    return "对话 · " + (task.issue_key || path.basename(task.case_path || "BugAgent"));
  }

  post(message) {
    if (this.view) this.view.webview.postMessage(message);
  }

  async handle(message) {
    try {
      const api = await this.server.client();
      if (message.type === "openConversation") {
        await this.open(await api.getConversation(String(message.conversationId || "")));
      } else if (message.type === "showHistory") {
        await this.refreshHistory();
      } else if (message.type === "newLocal") {
        await vscode.commands.executeCommand("bugAgent.newLocalCase");
      } else if (message.type === "newJira") {
        await vscode.commands.executeCommand("bugAgent.newJiraCase");
      } else if (message.type === "startInput") {
        await this.startFromInput(String(message.content || ""));
      } else if (message.type === "deleteConversation") {
        const id = String(message.conversationId || "");
        const hidden = this.hiddenConversationIds();
        hidden.add(id);
        await this.context.globalState.update("bugAgent.hiddenConversations", [...hidden]);
        if (this.record?.conversation_id === id) {
          this.abort?.abort();
          this.record = undefined;
          if (this.view) this.view.title = "BugAgent";
          this.post({type: "empty"});
        }
        await this.refreshHistory();
        this.post({type: "deleted", value: id});
      } else if (message.type === "undoDelete") {
        const hidden = this.hiddenConversationIds();
        hidden.delete(String(message.conversationId || ""));
        await this.context.globalState.update("bugAgent.hiddenConversations", [...hidden]);
        await this.refreshHistory();
      } else if (message.type === "retryConnection") {
        await this.server.ensure(this.record?.case_root || this.record?.task?.case_path);
        await this.refreshHistory();
        if (this.record) this.stream();
      } else if (message.type === "send" && this.record) {
        const pending = await api.sendMessage(
          this.record.conversation_id, String(message.content || ""),
        );
        this.post({type: "pending", value: pending});
        this.onChanged();
      } else if (message.type === "cancel" && this.record) {
        await api.cancelMessage(this.record.conversation_id, Number(message.messageId));
      } else if (message.type === "openLocation" && this.record) {
        const task = this.record.task || {};
        const config = vscode.workspace.getConfiguration("bugAgent");
        let jiraRoot = config.get("jiraExportRoot", "");
        if (jiraRoot && task.issue_key) jiraRoot = path.join(jiraRoot, task.issue_key);
        if (message.annotationMessage) {
          await this.annotations.syncMessage(this.record, message.annotationMessage);
        }
        const editor = await this.logOpener.open(
          message.location,
          this.record.case_root || task.case_path,
          jiraRoot,
        );
        await this.annotations.apply(editor);
      } else if (message.type === "openExternal") {
        const target = new URL(String(message.href || ""));
        if (!new Set(["https:", "http:"]).has(target.protocol)) {
          throw new Error("仅允许打开 HTTP/HTTPS 链接。");
        }
        await vscode.env.openExternal(vscode.Uri.parse(target.toString()));
      } else if (message.type === "refresh" && this.record) {
        this.record = await api.getConversation(this.record.conversation_id);
        await this.annotations.syncRecord(this.record);
        this.post({type: "conversation", value: this.record});
      }
    } catch (error) {
      vscode.window.showErrorMessage("BugAgent: " + error.message);
      this.post({type: "error", value: error.message});
    }
  }

  async startFromInput(raw) {
    const content = raw.trim();
    if (!content) return;
    const jira = content.match(/\b[A-Za-z][A-Za-z0-9_]*-\d+\b/);
    if (jira) {
      await vscode.commands.executeCommand("bugAgent.newJiraCase", {
        issueKey: jira[0].toUpperCase(), objective: content, sendImmediately: true,
      });
      return;
    }
    const candidate = content.replace(/^['"]|['"]$/g, "");
    if (path.isAbsolute(candidate) && fs.existsSync(candidate)) {
      await vscode.commands.executeCommand("bugAgent.newLocalCase", {
        casePath: candidate, objective: "定位 Bug 根因并给出下一步建议",
        sendImmediately: false,
      });
      return;
    }
    this.post({
      type: "inputHint",
      value: "请在描述中包含 Jira 编号，粘贴本地 Case 路径，或通过＋选择分析来源。",
    });
  }

  async stream() {
    this.abort?.abort();
    if (!this.record || !this.view) return;
    const conversationId = this.record.conversation_id;
    const abort = new AbortController();
    this.abort = abort;
    while (!abort.signal.aborted && this.record?.conversation_id === conversationId) {
      try {
        const api = await this.server.client();
        this.post({type: "connection", value: "已连接"});
        await api.streamEvents(
          conversationId,
          this.cursor,
          abort.signal,
          (event) => {
            if (abort.signal.aborted || this.record?.conversation_id !== conversationId) return;
            this.cursor = Math.max(this.cursor, event.event_id || 0);
            this.post({type: "event", value: event});
            if (event.kind === "assistant_message") {
              const annotationMessage = {
                message_id: event.message_id,
                report: event.report,
                locations: event.locations || [],
              };
              this.annotations.syncMessage(this.record, annotationMessage)
                .catch((error) => vscode.window.showWarningMessage(
                  "BugAgent 注释保存失败：" + error.message,
                ));
            }
            this.onChanged();
          },
        );
      } catch (error) {
        if (abort.signal.aborted) return;
        this.lastError = error.message;
        this.post({type: "connection", value: "连接中断，正在重连…"});
        this.post({type: "connectionError", value: error.message});
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
  }

  html(webview, media) {
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel-v3.css"));
    const fileCardsStyle = webview.asWebviewUri(vscode.Uri.joinPath(media, "file-cards.css"));
    const markdownStyle = webview.asWebviewUri(vscode.Uri.joinPath(media, "markdown.css"));
    const markdownScript = webview.asWebviewUri(vscode.Uri.joinPath(
      this.context.extensionUri, "node_modules", "markdown-it", "dist", "markdown-it.min.js",
    ));
    const nonce = String(Date.now());
    return [
      "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">",
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
      "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; ",
      "style-src " + webview.cspSource + "; script-src 'nonce-" + nonce + "';\">",
      "<link rel=\"stylesheet\" href=\"" + style + "\">",
      "<link rel=\"stylesheet\" href=\"" + fileCardsStyle + "\">",
      "<link rel=\"stylesheet\" href=\"" + markdownStyle + "\"></head><body>",
      "<div id=\"app\"><section id=\"history-screen\" class=\"screen hidden\">",
      "<header class=\"history-header\"><div><div class=\"eyebrow\">BUGAGENT</div><h1>历史会话</h1></div>",
      "<button id=\"history-close\" class=\"ghost icon-button\" aria-label=\"关闭历史记录\">×</button></header>",
      "<div class=\"history-actions\"><button id=\"history-new\" class=\"primary wide\">＋ 新建分析</button></div>",
      "<div id=\"history-list\" class=\"history-list\"></div></section>",
      "<section id=\"chat-screen\" class=\"screen\"><header class=\"case-header\">",
      "<button id=\"history-open\" class=\"ghost icon-button\" title=\"历史会话\" aria-label=\"历史会话\">☰</button>",
      "<div class=\"case-copy\"><h1 id=\"title\">BugAgent</h1><div id=\"case-meta\" class=\"case-meta\">工程调查助手</div></div>",
      "<span id=\"connection-pill\" class=\"connection-pill connecting\"><i></i><b>连接中</b></span>",
      "<button id=\"refresh\" class=\"ghost icon-button\" title=\"刷新会话\" aria-label=\"刷新会话\">↻</button></header>",
      "<div id=\"connection-banner\" class=\"connection-banner hidden\"><span id=\"connection-copy\"></span>",
      "<button id=\"retry\" class=\"text-button\">立即重试</button><button id=\"error-details\" class=\"text-button\">详情</button></div>",
      "<main id=\"messages\"></main>",
      "<footer class=\"composer\"><div id=\"run-strip\" class=\"run-strip hidden\"><span class=\"spinner\"></span>",
      "<span id=\"status\">正在分析</span><span id=\"elapsed\">0:00</span><button id=\"cancel\" class=\"text-button\" disabled>停止</button></div>",
      "<div class=\"composer-box\"><textarea id=\"input\" rows=\"1\" placeholder=\"输入 Jira 编号、Case 路径或问题…\"></textarea>",
      "<div class=\"composer-actions\"><div class=\"add-wrap\"><button id=\"add\" class=\"ghost round\" aria-label=\"新建分析\">＋</button>",
      "<div id=\"add-menu\" class=\"add-menu hidden\"><button id=\"new-jira\">Jira Issue<span>输入问题编号</span></button>",
      "<button id=\"new-local\">本地 Case<span>选择日志目录</span></button></div></div>",
      "<span class=\"composer-hint\">Enter 发送 · Shift+Enter 换行</span><button id=\"send\" class=\"send-button\" aria-label=\"发送\">↑</button>",
      "</div></div></footer></section><div id=\"toast\" class=\"toast hidden\"><span>会话已删除</span><button id=\"undo-delete\">撤销</button></div></div>",
      "<script nonce=\"" + nonce + "\" src=\"" + markdownScript + "\"></script>",
      "<script nonce=\"" + nonce + "\" src=\"" + script + "\"></script>",
      "</body></html>",
    ].join("");
  }
}

module.exports = {ConversationPanel};
