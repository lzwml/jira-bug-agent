"use strict";

const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const SCHEMA_VERSION = 1;
const GENERATED_BY = "bugagent-vscode";

function canonical(filePath) {
  return path.resolve(String(filePath || "")).toLowerCase();
}

function inside(root, target) {
  const relative = path.relative(path.resolve(root), path.resolve(target));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function annotationId(conversationId, evidenceId, relation, claim) {
  return "ann-" + crypto.createHash("sha256")
    .update([conversationId, evidenceId, relation, claim].join("|"))
    .digest("hex").slice(0, 16);
}

async function fingerprint(filePath) {
  const stat = await fs.promises.stat(filePath);
  const handle = await fs.promises.open(filePath, "r");
  try {
    const chunkSize = Math.min(64 * 1024, stat.size);
    const first = Buffer.alloc(chunkSize);
    const last = Buffer.alloc(chunkSize);
    if (chunkSize) {
      await handle.read(first, 0, chunkSize, 0);
      await handle.read(last, 0, chunkSize, Math.max(0, stat.size - chunkSize));
    }
    return {
      size: stat.size,
      mtime_ms: Math.trunc(stat.mtimeMs),
      sample_sha256: crypto.createHash("sha256").update(first).update(last).digest("hex"),
    };
  } finally {
    await handle.close();
  }
}

async function replaceFile(temporary, target) {
  try {
    await fs.promises.rename(temporary, target);
    return;
  } catch (error) {
    // Windows and cloud-sync providers commonly reject rename-over-existing with EPERM.
    // This snapshot is reproducible, so fall back to an overwrite without deleting the
    // last good target first.  A failed copy leaves that target in place.
    if (!["EPERM", "EEXIST", "ENOTEMPTY"].includes(error.code)) throw error;
  }
  await fs.promises.copyFile(temporary, target);
  await fs.promises.unlink(temporary);
}

function sameFingerprint(left, right) {
  return Boolean(left && right) && left.size === right.size &&
    left.mtime_ms === right.mtime_ms && left.sample_sha256 === right.sample_sha256;
}

function annotationsFromMessage(message, conversationId) {
  const report = message && message.report;
  if (!report) return [];
  const locations = new Map((message.locations || []).map((item) => [item.identifier, item]));
  const evidence = new Map((report.evidence || []).map((item) => [item.evidence_id, item]));
  const results = [];
  const add = (ids, relation, claim, status) => {
    for (const evidenceId of ids || []) {
      const item = evidence.get(evidenceId);
      const location = locations.get(evidenceId);
      if (!item || !location || location.availability !== "ready" ||
          !location.resolved_path || !location.line_start) continue;
      results.push({
        annotation_id: annotationId(conversationId, evidenceId, relation, claim),
        generated_by: GENERATED_BY,
        evidence_id: evidenceId,
        relation,
        claim,
        status,
        summary: item.excerpt || location.excerpt || "该行被 RCA 引用为关键证据。",
        relative_path: location.relative_path,
        resolved_path: location.resolved_path,
        line_start: location.line_start,
        line_end: location.line_end || location.line_start,
        conversation_id: conversationId,
        message_id: message.message_id,
      });
    }
  };
  if (report.root_cause) {
    add(
      report.root_cause_evidence_ids,
      report.conclusion_status === "confirmed" ? "confirmed" : "supports",
      report.root_cause,
      report.conclusion_status,
    );
  }
  for (const hypothesis of report.hypotheses || []) {
    add(hypothesis.supporting_evidence_ids, "supports", hypothesis.statement, hypothesis.status);
    add(hypothesis.contradicting_evidence_ids, "contradicts", hypothesis.statement, hypothesis.status);
  }
  return results;
}

class EvidenceAnnotationManager {
  constructor(context, largeLogs) {
    this.context = context;
    this.largeLogs = largeLogs;
    this.enabled = context.workspaceState.get("bugAgent.annotationsVisible", true);
    this.byFile = new Map();
    this.caseFiles = new Map();
    this.persistQueue = Promise.resolve();
    const gutter = vscode.Uri.joinPath(context.extensionUri, "media", "evidence-marker.svg");
    this.decorations = {
      confirmed: this.createDecoration("rgba(46, 160, 67, 0.10)", "#3fb950", gutter),
      supports: this.createDecoration("rgba(56, 139, 253, 0.10)", "#58a6ff", gutter),
      contradicts: this.createDecoration("rgba(210, 153, 34, 0.12)", "#d29922", gutter),
      stale: this.createDecoration("rgba(139, 148, 158, 0.08)", "#8b949e", gutter),
    };
    context.subscriptions.push(
      ...Object.values(this.decorations),
      vscode.window.onDidChangeVisibleTextEditors(() => this.refreshVisible()),
      vscode.workspace.onDidChangeTextDocument((event) => this.refreshDocument(event.document)),
    );
  }

  createDecoration(backgroundColor, borderColor, gutterIconPath) {
    return vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor,
      borderWidth: "0 0 0 2px",
      borderStyle: "solid",
      borderColor,
      gutterIconPath,
      gutterIconSize: "contain",
      overviewRulerColor: borderColor,
      overviewRulerLane: vscode.OverviewRulerLane.Right,
      after: {margin: "0 0 0 1.5rem", color: borderColor, fontStyle: "italic"},
    });
  }

  async toggle() {
    this.enabled = !this.enabled;
    await this.context.workspaceState.update("bugAgent.annotationsVisible", this.enabled);
    this.refreshVisible();
    vscode.window.showInformationMessage(
      "BugAgent 证据注释已" + (this.enabled ? "显示" : "隐藏") + "。",
    );
  }

  async syncRecord(record) {
    const caseRoot = record.case_root || (record.task || {}).case_path;
    if (!caseRoot) return;
    const generated = (record.messages || []).flatMap((message) =>
      annotationsFromMessage(message, record.conversation_id));
    await this.enqueuePersist(caseRoot, generated);
  }

  async syncMessage(record, message) {
    const caseRoot = record.case_root || (record.task || {}).case_path;
    if (!caseRoot) return;
    await this.enqueuePersist(
      caseRoot,
      annotationsFromMessage(message, record.conversation_id),
    );
  }

  enqueuePersist(caseRoot, generated) {
    const operation = this.persistQueue.then(() => this.persist(caseRoot, generated));
    // Keep the queue usable after one failed write; callers still receive the failure.
    this.persistQueue = operation.catch(() => undefined);
    return operation;
  }

  async persist(caseRoot, generated) {
    const resolvedRoot = path.resolve(caseRoot);
    const agentDir = path.join(resolvedRoot, ".bug-agent");
    const target = path.join(agentDir, "evidence-annotations.json");
    if (!inside(resolvedRoot, target)) return;
    await fs.promises.mkdir(agentDir, {recursive: true});
    await this.cleanupOrphans(agentDir);
    let stored = {schema_version: SCHEMA_VERSION, annotations: []};
    let targetExists = true;
    try {
      stored = JSON.parse(await fs.promises.readFile(target, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      targetExists = false;
    }
    const merged = new Map((stored.annotations || []).map((item) => [item.annotation_id, item]));
    let changed = false;
    for (const item of generated) {
      if (!inside(resolvedRoot, item.resolved_path)) continue;
      try {
        item.source_fingerprint = await fingerprint(item.resolved_path);
      } catch {
        continue;
      }
      const previous = merged.get(item.annotation_id);
      if (JSON.stringify(previous) !== JSON.stringify(item)) changed = true;
      merged.set(item.annotation_id, item);
    }
    if (!changed && targetExists) {
      this.caseFiles.set(resolvedRoot, target);
      await this.loadFile(target);
      this.refreshVisible();
      return;
    }
    if (!generated.length && !targetExists) return;
    const payload = {
      schema_version: SCHEMA_VERSION,
      updated_at: new Date().toISOString(),
      annotations: [...merged.values()],
    };
    const temporary = target + "." + crypto.randomUUID() + ".tmp";
    try {
      await fs.promises.writeFile(temporary, JSON.stringify(payload, null, 2) + "\n", "utf8");
      await replaceFile(temporary, target);
    } finally {
      await fs.promises.unlink(temporary).catch((error) => {
        if (error.code !== "ENOENT") throw error;
      });
    }
    this.caseFiles.set(resolvedRoot, target);
    await this.loadFile(target);
    this.refreshVisible();
  }

  async cleanupOrphans(agentDir) {
    const prefix = "evidence-annotations.json.";
    const cutoff = Date.now() - 60_000;
    const names = await fs.promises.readdir(agentDir).catch((error) => {
      if (error.code === "ENOENT") return [];
      throw error;
    });
    await Promise.all(names.filter((name) => name.startsWith(prefix) && name.endsWith(".tmp"))
      .map(async (name) => {
        const candidate = path.join(agentDir, name);
        const stat = await fs.promises.stat(candidate).catch(() => undefined);
        if (stat && stat.mtimeMs < cutoff) {
          await fs.promises.unlink(candidate).catch(() => undefined);
        }
      }));
  }

  async loadFile(filePath) {
    const payload = JSON.parse(await fs.promises.readFile(filePath, "utf8"));
    for (const item of payload.annotations || []) {
      if (!item.resolved_path || !item.line_start) continue;
      const key = canonical(item.resolved_path);
      const current = this.byFile.get(key) || [];
      const index = current.findIndex((value) => value.annotation_id === item.annotation_id);
      if (index >= 0) current[index] = item;
      else current.push(item);
      this.byFile.set(key, current);
    }
  }

  async refreshVisible() {
    await Promise.all(vscode.window.visibleTextEditors.map((editor) => this.apply(editor)));
  }

  async refreshDocument(document) {
    const editor = vscode.window.visibleTextEditors.find((item) => item.document === document);
    if (editor) await this.apply(editor);
  }

  clear(editor) {
    for (const decoration of Object.values(this.decorations)) {
      editor.setDecorations(decoration, []);
    }
  }

  async apply(editor) {
    this.clear(editor);
    if (!this.enabled) return;
    const metadata = this.largeLogs.metadata(editor.document.uri);
    const filePath = metadata ? metadata.filePath :
      editor.document.uri.scheme === "file" ? editor.document.uri.fsPath : undefined;
    if (!filePath) return;
    const annotations = this.byFile.get(canonical(filePath)) || [];
    if (!annotations.length) return;
    let currentFingerprint;
    try {
      currentFingerprint = await fingerprint(filePath);
    } catch {
      return;
    }
    const grouped = {confirmed: [], supports: [], contradicts: [], stale: []};
    const labels = {
      confirmed: "已确认根因证据",
      supports: "支持候选结论",
      contradicts: "反驳候选结论",
      stale: "日志已变化，注释可能过期",
    };
    const lineAnnotations = new Map();
    for (const item of annotations) {
      const stale = !sameFingerprint(item.source_fingerprint, currentFingerprint);
      let sourceLine = item.line_start;
      if (metadata) {
        if (sourceLine < metadata.first || sourceLine > metadata.last) continue;
        sourceLine = sourceLine - metadata.first + 4;
      }
      const line = Math.min(editor.document.lineCount - 1, Math.max(0, sourceLine - 1));
      const relation = stale ? "stale" : (grouped[item.relation] ? item.relation : "supports");
      const existing = lineAnnotations.get(line) || [];
      existing.push({item, relation});
      lineAnnotations.set(line, existing);
    }
    const rank = {stale: 4, confirmed: 3, contradicts: 2, supports: 1};
    for (const [line, entries] of lineAnnotations) {
      const relation = entries.map((entry) => entry.relation)
        .sort((left, right) => rank[right] - rank[left])[0];
      const hover = new vscode.MarkdownString(undefined, true);
      hover.appendMarkdown("**BugAgent 证据注释**  \n");
      hover.appendMarkdown("关系：" + labels[relation] + "  \n");
      for (const {item, relation: itemRelation} of entries) {
        hover.appendMarkdown("\n- **" + labels[itemRelation] + "**：" +
          String(item.claim || "未记录") + "  \n  证据：`" +
          String(item.evidence_id || "-") + "`；说明：" +
          String(item.summary || "-") + "；原始位置：L" + item.line_start +
          (item.line_end !== item.line_start ? "–L" + item.line_end : ""));
      }
      hover.isTrusted = false;
      const firstClaim = String(entries[0].item.claim || "关键证据").replace(/\s+/g, " ");
      const shortClaim = firstClaim.length > 42 ? firstClaim.slice(0, 42) + "…" : firstClaim;
      const more = entries.length > 1 ? "（另有 " + String(entries.length - 1) + " 条关联）" : "";
      grouped[relation].push({
        range: new vscode.Range(line, 0, line, editor.document.lineAt(line).text.length),
        hoverMessage: hover,
        renderOptions: {after: {contentText: "  BugAgent：" + labels[relation] +
          " · " + shortClaim + more}},
      });
    }
    for (const [relation, entries] of Object.entries(grouped)) {
      editor.setDecorations(this.decorations[relation], entries);
    }
  }
}

module.exports = {
  EvidenceAnnotationManager,
  annotationsFromMessage,
  fingerprint,
  replaceFile,
  sameFingerprint,
};
