"use strict";

const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const readline = require("readline");

class LargeLogProvider {
  constructor() {
    this.documents = new Map();
  }

  provideTextDocumentContent(uri) {
    return this.documents.get(uri.toString()) || "日志片段不可用。";
  }

  async create(filePath, lineStart, lineEnd) {
    const first = Math.max(1, (lineStart || 1) - 200);
    const last = (lineEnd || lineStart || 1) + 200;
    const rows = [];
    let number = 0;
    const input = fs.createReadStream(filePath, {encoding: "utf8"});
    const reader = readline.createInterface({input, crlfDelay: Infinity});
    for await (const line of reader) {
      number += 1;
      if (number >= first) rows.push(String(number).padStart(8) + "  " + line);
      if (number >= last) {
        reader.close();
        input.destroy();
        break;
      }
    }
    const uri = vscode.Uri.parse(
      "bugagent-log:" + encodeURIComponent(filePath) +
      "?start=" + String(first) + "&target=" + String(lineStart || 1),
    );
    this.documents.set(
      uri.toString(),
      "只读日志片段：" + filePath + "\n显示 L" + first + "-L" + number + "\n\n" +
      rows.join("\n"),
    );
    return {uri, first};
  }
}

class LogOpener {
  constructor(provider) {
    this.provider = provider;
  }

  async open(location, casePath, jiraExportRoot) {
    const rawPath = location.relative_path || "";
    let filePath = path.isAbsolute(rawPath) ? rawPath : undefined;
    if (!filePath && casePath) filePath = path.resolve(casePath, rawPath);
    if (!filePath && jiraExportRoot) filePath = path.resolve(jiraExportRoot, rawPath);
    if (!filePath || !fs.existsSync(filePath)) {
      throw new Error("文件尚未提取或本机不可访问：" + rawPath);
    }
    const stat = fs.statSync(filePath);
    const threshold = vscode.workspace
      .getConfiguration("bugAgent")
      .get("largeFileThresholdMB", 100) * 1024 * 1024;
    if (stat.size > threshold) {
      const fragment = await this.provider.create(
        filePath, location.line_start, location.line_end,
      );
      const document = await vscode.workspace.openTextDocument(fragment.uri);
      await vscode.window.showTextDocument(document, {
        viewColumn: vscode.ViewColumn.Beside, preview: true,
      });
      return;
    }
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
    const editor = await vscode.window.showTextDocument(document, {
      viewColumn: vscode.ViewColumn.Beside, preview: true,
    });
    const start = Math.max(0, (location.line_start || 1) - 1);
    const end = Math.max(start, (location.line_end || location.line_start || 1) - 1);
    const range = new vscode.Range(start, 0, end, document.lineAt(end).text.length);
    editor.selection = new vscode.Selection(range.start, range.end);
    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
  }
}

module.exports = {LargeLogProvider, LogOpener};
