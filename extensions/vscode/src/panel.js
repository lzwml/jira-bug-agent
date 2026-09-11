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
    this.panels = new Map();
  }

  async open(record) {
    await this.annotations.syncRecord(record);
    const existing = this.panels.get(record.conversation_id);
    if (existing) {
      existing.panel.reveal();
      return;
    }
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    const markdownRoot = vscode.Uri.joinPath(
      this.context.extensionUri, "node_modules", "markdown-it", "dist",
    );
    const panel = vscode.window.createWebviewPanel(
      "bugAgent.conversation",
      this.title(record),
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [media, markdownRoot],
      },
    );
    panel.webview.html = this.html(panel.webview, media);
    const state = {panel, record, abort: new AbortController(), cursor: 0};
    this.panels.set(record.conversation_id, state);

    panel.onDidDispose(() => {
      state.abort.abort();
      this.panels.delete(record.conversation_id);
    });
    panel.webview.onDidReceiveMessage((message) => this.handle(state, message));
    panel.webview.postMessage({type: "conversation", value: record});
    this.stream(state);
  }

  title(record) {
    const task = record.task || {};
    return "BugAgent · " + (task.issue_key || path.basename(task.case_path || "会话"));
  }

  async handle(state, message) {
    try {
      const api = await this.server.client();
      if (message.type === "send") {
        const pending = await api.sendMessage(
          state.record.conversation_id, String(message.content || ""),
        );
        state.panel.webview.postMessage({type: "pending", value: pending});
        this.onChanged();
      } else if (message.type === "cancel") {
        await api.cancelMessage(state.record.conversation_id, Number(message.messageId));
      } else if (message.type === "openLocation") {
        const task = state.record.task || {};
        const config = vscode.workspace.getConfiguration("bugAgent");
        let jiraRoot = config.get("jiraExportRoot", "");
        if (jiraRoot && task.issue_key) jiraRoot = path.join(jiraRoot, task.issue_key);
        if (message.annotationMessage) {
          await this.annotations.syncMessage(state.record, message.annotationMessage);
        }
        const editor = await this.logOpener.open(
          message.location,
          state.record.case_root || task.case_path,
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
        state.record = await api.getConversation(state.record.conversation_id);
        state.panel.webview.postMessage({type: "conversation", value: state.record});
      }
    } catch (error) {
      vscode.window.showErrorMessage("BugAgent: " + error.message);
      state.panel.webview.postMessage({type: "error", value: error.message});
    }
  }

  async stream(state) {
    while (!state.abort.signal.aborted) {
      try {
        const api = await this.server.client();
        state.panel.webview.postMessage({type: "connection", value: "已连接"});
        await api.streamEvents(
          state.record.conversation_id,
          state.cursor,
          state.abort.signal,
          (event) => {
            state.cursor = Math.max(state.cursor, event.event_id || 0);
            state.panel.webview.postMessage({type: "event", value: event});
            if (event.kind === "assistant_message") {
              const annotationMessage = {
                message_id: event.message_id,
                report: event.report,
                locations: event.locations || [],
              };
              this.annotations.syncMessage(state.record, annotationMessage)
                .catch((error) => vscode.window.showWarningMessage(
                  "BugAgent 注释保存失败：" + error.message,
                ));
            }
            this.onChanged();
          },
        );
      } catch (error) {
        if (state.abort.signal.aborted) return;
        state.panel.webview.postMessage({
          type: "connection", value: "连接中断，正在重连…",
        });
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
      "<button id=\"refresh\">刷新</button></header>",
      "<main id=\"messages\"></main>",
      "<footer><textarea id=\"input\" placeholder=\"补充线索或继续追问（Ctrl+Enter 发送）\"></textarea>",
      "<div><button id=\"cancel\" disabled>取消本轮</button>",
      "<button id=\"send\">发送</button></div></footer>",
      "<script nonce=\"" + nonce + "\" src=\"" + markdownScript + "\"></script>",
      "<script nonce=\"" + nonce + "\" src=\"" + script + "\"></script>",
      "</body></html>",
    ].join("");
  }
}

/* duplicate removed */
/*
  html(webview, media) {
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
    const nonce = String(Date.now());
    return "<!doctype html><html lang='zh-CN'><head>" +
      "<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
      "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; " +
      "style-src " + webview.cspSource + "; script-src 'nonce-" + nonce + "';\">" +
      "<link rel='stylesheet' href='" + style + "'><title>BugAgent</title></head>" +
      "<body><header><div><h1 id='title'>BugAgent</h1><span id='status'>连接中…</span></div>" +
      "<button id='refresh'>刷新</button></header><main id='messages'></main>" +
      "<footer><textarea id='input' placeholder='补充线索、提出问题或要求继续调查'></textarea>" +
      "<div><button id='cancel' disabled>取消本轮</button><button id='send'>发送</button></div></footer>" +
      "<script nonce='" + nonce + "' src='" + script + "'></script></body></html>";
  }
}
*/

module.exports = {ConversationPanel};
