"use strict";

const vscode = require("vscode");
const path = require("path");

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
    this.stream();
  }

  title(record) {
    const task = record.task || {};
    return "对话 · " + (task.issue_key || path.basename(task.case_path || "BugAgent"));
  }

  post(message) {
    if (this.view) this.view.webview.postMessage(message);
  }

  async handle(message) {
    if (!this.record) return;
    try {
      const api = await this.server.client();
      if (message.type === "send") {
        const pending = await api.sendMessage(
          this.record.conversation_id, String(message.content || ""),
        );
        this.post({type: "pending", value: pending});
        this.onChanged();
      } else if (message.type === "cancel") {
        await api.cancelMessage(this.record.conversation_id, Number(message.messageId));
      } else if (message.type === "openLocation") {
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
      } else if (message.type === "refresh") {
        this.record = await api.getConversation(this.record.conversation_id);
        await this.annotations.syncRecord(this.record);
        this.post({type: "conversation", value: this.record});
      }
    } catch (error) {
      vscode.window.showErrorMessage("BugAgent: " + error.message);
      this.post({type: "error", value: error.message});
    }
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
      } catch {
        if (abort.signal.aborted) return;
        this.post({type: "connection", value: "连接中断，正在重连…"});
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
  }

  html(webview, media) {
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
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
      "<header><h1 id=\"title\">BugAgent</h1><span id=\"status\">连接中…</span>",
      "<button id=\"refresh\" title=\"刷新会话\">↻</button></header>",
      "<main id=\"messages\"><div class=\"empty-state\">从 Case 与会话列表中选择一项，这里会持续显示分析过程。</div></main>",
      "<footer><textarea id=\"input\" placeholder=\"补充线索或继续追问（Ctrl+Enter 发送）\"></textarea>",
      "<div><button id=\"cancel\" disabled>取消</button>",
      "<button id=\"send\" disabled>发送</button></div></footer>",
      "<script nonce=\"" + nonce + "\" src=\"" + markdownScript + "\"></script>",
      "<script nonce=\"" + nonce + "\" src=\"" + script + "\"></script>",
      "</body></html>",
    ].join("");
  }
}

module.exports = {ConversationPanel};
