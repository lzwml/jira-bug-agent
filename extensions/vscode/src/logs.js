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
    let filePath = location.resolved_path || undefined;
    if (!filePath && rawPath.includes("!/")) {
      const archive = location.archive_relative_path || rawPath.split("!/")[0];
      const member = location.member_path || rawPath.split("!/").slice(1).join("!/");
      throw new Error(
        "该归档成员尚未提取，暂时不能直接打开。归档：" + archive +
        (member ? "；成员：" + member : ""),
      );
    }
    if (!filePath && path.isAbsolute(rawPath)) filePath = rawPath;
    const roots = [location.case_root, casePath, jiraExportRoot].filter(Boolean);
    for (const root of roots) {
      if (filePath) break;
      const resolvedRoot = path.resolve(root);
      const candidate = path.resolve(resolvedRoot, rawPath);
      const relative = path.relative(resolvedRoot, candidate);
      if (!relative.startsWith("..") && !path.isAbsolute(relative)) filePath = candidate;
    }
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
      const editor = await vscode.window.showTextDocument(document, {
        viewColumn: vscode.ViewColumn.Beside, preview: true,
      });
      const start = Math.min(
        document.lineCount - 1,
        Math.max(0, (location.line_start || fragment.first) - fragment.first + 3),
      );
      const end = Math.min(
        document.lineCount - 1,
        Math.max(start, (location.line_end || location.line_start || fragment.first) -
          fragment.first + 3),
      );
      const range = new vscode.Range(start, 0, end, document.lineAt(end).text.length);
      editor.selection = new vscode.Selection(range.start, range.end);
      editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
      return;
    }
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
    const editor = await vscode.window.showTextDocument(document, {
      viewColumn: vscode.ViewColumn.Beside, preview: true,
    });
    const lastLine = Math.max(0, document.lineCount - 1);
    const start = Math.min(lastLine, Math.max(0, (location.line_start || 1) - 1));
    const end = Math.min(
      lastLine,
      Math.max(start, (location.line_end || location.line_start || 1) - 1),
    );
    const range = new vscode.Range(start, 0, end, document.lineAt(end).text.length);
    editor.selection = new vscode.Selection(range.start, range.end);
    editor.revealRange(range, vscode.TextEditorRevealType.InCenter);
  }
}

module.exports = {LargeLogProvider, LogOpener};
