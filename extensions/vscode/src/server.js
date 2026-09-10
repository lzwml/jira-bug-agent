"use strict";

const vscode = require("vscode");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const {spawn} = require("child_process");
const {BugAgentApi} = require("./api");

const SECRET_KEY = "bugAgent.apiKey";

class ServerManager {
  constructor(context, output) {
    this.context = context;
    this.output = output;
    this.process = undefined;
    this.allowedRoots = new Set();
  }

  get config() {
    return vscode.workspace.getConfiguration("bugAgent");
  }

  async apiKey() {
    let key = await this.context.secrets.get(SECRET_KEY);
    if (!key) {
      key = crypto.randomBytes(24).toString("hex");
      await this.context.secrets.store(SECRET_KEY, key);
    }
    return key;
  }

  async client() {
    const serverUrl = this.config.get("serverUrl", "http://127.0.0.1:8000");
    const parsed = new URL(serverUrl);
    const localHosts = new Set(["127.0.0.1", "localhost", "[::1]"]);
    if (!localHosts.has(parsed.hostname) && parsed.protocol !== "https:") {
      throw new Error("远程 BugAgent 服务必须使用 HTTPS。");
    }
    return new BugAgentApi(serverUrl, await this.apiKey());
  }

  findRepository() {
    const configured = this.config.get("repositoryPath", "");
    if (configured && fs.existsSync(path.join(configured, "pyproject.toml"))) {
      return configured;
    }
    for (const folder of vscode.workspace.workspaceFolders || []) {
      if (fs.existsSync(path.join(folder.uri.fsPath, "src", "bug_agent"))) {
        return folder.uri.fsPath;
      }
    }
    const root = path.resolve(this.context.extensionPath, "..", "..");
    return fs.existsSync(path.join(root, "pyproject.toml")) ? root : undefined;
  }

  async ensure(caseRoot) {
    let addedRoot = false;
    if (caseRoot) {
      const resolved = path.resolve(caseRoot);
      addedRoot = !this.allowedRoots.has(resolved);
      this.allowedRoots.add(resolved);
    }
    if (addedRoot && this.process) {
      this.stop();
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    const client = await this.client();
    try {
      await client.health();
      return client;
    } catch {}
    if (!this.config.get("autoStart", true)) {
      throw new Error("BugAgent 服务不可用，且自动启动已关闭。");
    }
    await this.start();
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      try {
        await client.health();
        return client;
      } catch {}
    }
    throw new Error("BugAgent 服务启动超时，请查看 BugAgent 输出。");
  }

  async start() {
    if (this.process) return;
    const repository = this.findRepository();
    if (!repository) {
      throw new Error("未找到 jira-bug-agent 仓库，请配置 bugAgent.repositoryPath。");
    }
    const key = await this.apiKey();
    const configuredRoots = this.config.get("allowedLocalRoots", []);
    for (const root of configuredRoots) this.allowedRoots.add(path.resolve(root));
    const env = {
      ...process.env,
      BUG_AGENT_API_KEY: key,
      BUG_AGENT_API_ALLOWED_LOCAL_ROOTS: [...this.allowedRoots].join(";"),
    };
    const command = this.config.get("launchCommand", "uv");
    const args = this.config.get("launchArguments", ["run", "bug-agent-api"]);
    this.output.appendLine("启动 BugAgent: " + command + " " + args.join(" "));
    const child = spawn(command, args, {
      cwd: repository, env, windowsHide: true, shell: false,
    });
    this.process = child;
    child.stdout.on("data", (value) => this.output.append(value.toString()));
    child.stderr.on("data", (value) => this.output.append(value.toString()));
    child.on("exit", (code) => {
      this.output.appendLine("BugAgent 服务已退出: " + String(code));
      if (this.process === child) this.process = undefined;
    });
  }

  stop() {
    if (this.process) {
      this.process.kill();
      this.process = undefined;
    }
  }
}

module.exports = {ServerManager, SECRET_KEY};
