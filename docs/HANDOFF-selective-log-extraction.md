# Jira Bug Agent 新会话交接

更新时间：2026-08-31

## 新会话建议开场

请先阅读本文件，然后继续维护 `jira-bug-agent`。当前阶段重点是完成并硬化
“归档清单 → 选择性解压 → 渐进索引”流程。保留现有用户改动，不要 reset 工作树；
开始修改前先运行本文的测试命令。

## 项目目标

这是完整的 Jira/本地 Android Bug 分析 Agent，不是单独的 Jira MCP。总体分层：

- Jira MCP：采集 Issue、评论、关联 Issue、附件及原始上下文。
- Agent/Worker：理解 Jira 症状、问题时间、日志域并制定调查顺序。
- Log MCP：受控清点归档、选择性解压、索引、搜索、时间线和诊断解析。
- Skills：提供平台知识与按症状路由，不直接执行文件操作。

Jira Data Center 已验证可连接 `https://jira.bitech-auto.com`。附件导出支持评论中引用的
关联 Jira，并按附件 ID、文件名和实际大小复用已有下载。

## 当前解压与索引布局

归档内容放在归档旁边，便于人工查看：

```text
BAIC-41409/
├── attachments/
│   ├── android.zip
│   └── android.zip.unpacked/
│       ├── logs/
│       └── .extraction-manifest.json
└── .bug-agent/
    └── index/
        └── logs.sqlite3
```

- 解压目录固定为 `<归档文件名>.unpacked/`。
- CLI 默认把索引和内部状态放在 Case 下 `.bug-agent/`。
- `--work-dir` 只改变索引/内部状态位置，不改变解压位置。
- `open_case` 会剪枝跳过 `.bug-agent`、`.unpacked`、staging 和 backup 目录。
- 非 Agent 管理或含额外用户内容的同名目录不会被覆盖。

## 已实现的默认 Agent 流程

Agent Prompt、MCP 描述和 MTK Skill 已统一为：

```text
完整分页收集 Jira 描述和评论
→ 写入并校验评论完整性 Manifest
→ 小上下文直接注入完整原文；大上下文才由 Comment Compiler 有预算地分块压缩
→ 主 Agent 接收 DIRECT/COMPILED 上下文（必要时 get_case_comment 复验后精读原文）
→ open_case
→ inspect_case
→ inspect_archive
→ 结合 Jira 症状、问题时间、日志域、文件名和大小选择 member_id
→ extract_archive_members
→ build_index
→ search_evidence / extract_timeline / parse_diagnostics
→ 证据不足时分页清点并逐步扩围
→ 最后才使用 prepare_case 全量兜底
```

Jira 评论现在是 Worker 的硬前置条件。旧版导出 Case 缺少完整性元数据时会拒绝分析，
需要重新执行 `collect-jira <KEY> --export-case`；已有附件会按元数据复用。

2026-08-31 的硬化还包括：Jira 评论分页无进展、重复 ID 或 total 变化时返回可重试
错误；`get_case_comment` 每次读取都复验 Manifest、完整性元数据和 `issue.json` 哈希；
Jira 导出与校验先于 Log MCP 和 Provider 启动；运行记录保存上下文模式、字符数、
编译分块/尝试/重试数及失败阶段，不保存原始评论副本或 Provider 响应正文。

以下仍是显式全量入口，不是 Agent 默认策略：

```powershell
uv run bug-agent prepare-local ".\exports\BAIC-41409"
uv run bug-agent prepare-local ".\exports\BAIC-41409" --no-index
uv run bug-agent collect-jira BAIC-41409 --prepare
```

不加 `--prepare` 的 `collect-jira` 不会解压。

## 新增 Log MCP 工具

### `inspect_archive`

输入：

```json
{
  "case_id": "case_xxx",
  "artifact_id": "artifact_xxx",
  "member_offset": 0,
  "max_members": 1000,
  "source_sha256": null
}
```

- 只读目录，不写出成员内容。
- 返回成员路径、大小、类型、安全状态和稳定 `member_id`。
- `member_id` 绑定 archive artifact、源归档 SHA-256 和规范成员路径。
- 分页时使用 `next_offset`，并把上一页 `source_fingerprint.sha256` 作为
  `source_sha256` 传回，防止跨页混合不同归档版本。

### `extract_archive_members`

输入：

```json
{
  "case_id": "case_xxx",
  "artifact_id": "artifact_archive",
  "member_ids": ["member_sha256"],
  "force_rebuild": false
}
```

- 只能使用 `inspect_archive` 返回的安全成员 ID。
- 多轮增量保留已选成员，不展开未选成员。
- 不自动递归；嵌套归档注册成新 Artifact 后必须再次 inspect/extract。
- 显式多轮调用也受 `max_depth` 限制。
- 文件数、累计展开字节和累计压缩比按该归档已选成员总集合计算。
- 清点和写盘后会复验源 SHA-256，避免 TOCTOU 版本混用。

### `build_index`

输入：

```json
{
  "case_id": "case_xxx",
  "artifact_ids": ["artifact_selected"],
  "force_rebuild": false
}
```

- 把请求 Artifact 累计加入该 Case 的索引集合。
- 当前底层仍采用临时数据库整体重建，而不是 SQLite 原位增量更新。
- 重建时优先处理历史已索引 Artifact，预算不足不会优先牺牲已有证据。
- 请求文件已经消失时明确返回 `ARTIFACT_UNAVAILABLE`。

## 已完成的安全与一致性保护

- ZIP、TAR、TAR.GZ、TGZ、单文件 GZIP。
- 拒绝绝对路径、盘符、`..`、Windows ADS/保留名、尾随点/空格。
- 拒绝符号/硬链接、特殊文件、加密 ZIP、重复路径和文件/目录前缀冲突。
- 拒绝归档成员占用 `.extraction-manifest.json`。
- 限制归档大小、成员大小、累计展开大小、成员数、压缩比、深度和运行时间。
- 每次读取 Artifact 都重新校验真实路径仍位于 Case 内，防止注册后路径被替换。
- Manifest 绑定源大小、mtime 和 SHA-256；full/selective 两种 Manifest 可互相迁移。
- 原子 staging、backup、Windows sharing violation 有限重试和失败回滚。
- 源归档变化、损坏或解压目录被篡改时，旧派生证据会失效。
- 普通扩围预算失败或无效成员请求不会删除此前有效证据。
- 选择性扩围优先用同卷硬链接克隆既有成员，不支持时逐文件回退复制；
  顶层 Manifest 始终独立复制，保留原子替换和失败回滚能力。

## 关键文件

- `packages/log-analyzer-mcp/src/log_analyzer/archive_manager.py`
  - 全量安全解压、full Manifest、通用路径安全。
- `packages/log-analyzer-mcp/src/log_analyzer/archive_selection.py`
  - inventory、版本化 member ID、选择性增量解压。
- `packages/log-analyzer-mcp/src/log_analyzer/case_registry.py`
  - Case/Artifact 注册、生成目录剪枝、路径生命周期校验。
- `packages/log-analyzer-mcp/src/log_analyzer/service.py`
  - 九个 Log MCP 工具的领域实现与渐进索引协调。
- `packages/log-analyzer-mcp/src/log_analyzer/domain.py`
  - MCP 输入契约。
- `packages/log-analyzer-mcp/src/log_analyzer/server.py`
  - MCP tools/list 与 tools/call 适配。
- `src/bug_agent/prompts.py`
  - Jira/Local Agent 默认选择性调查流程。
- `skills/mtk-ivi-log-analysis/SKILL.md`
  - MTK 平台路由入口。
- `skills/mtk-ivi-log-analysis/references/archive-safety.md`
  - 归档工具边界说明。

## 测试状态

最后一次完整回归：

```text
189 passed, 1 skipped
```

新会话首先重新运行完整命令确认环境状态：

```powershell
.\.venv\Scripts\python.exe -m pytest tests packages\log-analysis-core\tests packages\log-analyzer-mcp\tests packages\jira-bug-mcp\tests -q
```

Skill Creator 的 `quick_validate.py` 因当前运行时没有 `PyYAML` 无法执行；没有为此
污染项目依赖。`tests/test_skills.py` 已通过，可在新会话决定是否用独立环境补装验证。

## 建议下一步

按优先级：

1. 如需要真正高效的 lazy index，把 `LogIndex.build` 从累计集合整体重建升级为
   per-artifact fingerprint + SQLite 增量 upsert/delete。
2. 讨论是否增加 Case 级总展开配额；当前累计预算主要按单归档控制。
3. 讨论是否在 Worker 运行时隐藏/门控 `prepare_case`。目前“默认不全量”由 Prompt、
   Skill 和工具描述约束，`prepare_case` 仍作为显式兜底工具暴露。
4. 下一阶段再接 Jira 上下文时间推断：保留多个候选时间、来源、置信度和时区，
   由 Agent 用于成员选择与日志时间窗口，不依赖固定 customfield。

## 工作树注意事项

工作树包含 2026-08-31 的 Jira 上下文硬化修改，尚未提交。它们都属于当前项目进度，
不要使用 `git reset --hard`、`git checkout --` 或覆盖式回滚。先用 `git status --short`
和定向 diff 了解范围，再继续修改或提交。
