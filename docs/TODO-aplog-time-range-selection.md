# TODO：APLog 按事故时间选择归档

更新时间：2026-09-01

## 新会话开始前

请先阅读本文，并在 `jira-bug-agent` 仓库中执行：

```powershell
git status --short
git diff --check
git diff
```

不要 reset、restore 或覆盖当前工作区。当前有一组尚未提交的“跨分析复用已解压日志”修改：

- `packages/log-analyzer-mcp/README.md`
- `packages/log-analyzer-mcp/src/log_analyzer/archive_selection.py`
- `packages/log-analyzer-mcp/src/log_analyzer/service.py`
- `packages/log-analyzer-mcp/tests/test_preparation.py`
- `src/bug_agent/prompts.py`

这组修改已经验证：新 MCP 进程重新打开同一 Case 后，`inspect_archive` 可以从受管
Manifest 恢复已解压成员的 `artifact_id`，Agent 无需再次调用解压工具。相关测试结果：
`141 passed, 1 skipped`。建议先复核并单独提交这组修改，再实现本文的时间过滤能力。

最近相关提交：

```text
ab1c40f feat: improve bug analysis guidance and log coverage
8157bd9 feat: support iterative bug analysis tasks
```

## 问题描述

实际 Case 中，一个外层归档可能包含 44 个或更多 `APLog_*` 条目。Log MCP 的
`InspectArchiveInput.max_members` 默认上限足够，但完整 JSON 可能超过 Agent Harness 的
`max_tool_result_chars=40000`，随后被包装成 `tool_result_truncated=true` 的 preview。
模型看不到后半部分的 `member_path` 和安全 `member_id`，反复分页后容易丢失事故时间与
APLog 编号之间的关系。

核心问题不是归档无法分页，而是模型不应该承担 APLog 文件名解析、时间排序和邻卷选择。
这些应由确定性工具完成。

## 已确认的现有约束

1. `extract_archive_members` 只接受 `inspect_archive` 返回的稳定 `member_id`，不接受
   模型提供的裸 `member_path`。必须保留这条安全边界。
2. `search_evidence` 只能搜索已注册/已索引的文本，不能搜索尚未解压的归档成员名称。
3. Jira 描述与评论由 Worker 校验后通过 `DIRECT_JIRA_CONTEXT` 或
   `COMPILED_JIRA_CONTEXT` 提供；不要把 `issue.md` 当作唯一事故时间来源。
4. APLog 的起始时间可从命名中解析，但不能写死“每卷覆盖 1–2 分钟”。更可靠的候选
   区间是 `[start(i), start(i+1))`。
5. 例如事故时间为 `06:24:10`，`061532__79` 是事故点之前最近的卷，
   `062429__80` 是后继卷。两者起始时间相差约 9 分钟，因此固定时长假设不成立。

## 目标流程

```text
从已验证 Jira 上下文提取 reported incident time
              ↓
inspect_archive(time_range, neighbor_count=1)
              ↓
工具解析 APLog 名称、按起始时间排序
              ↓
返回前驱卷 / 范围内卷 / 后继卷（含稳定 member_id）
              ↓
已解压成员：直接复用 artifact_id
未解压成员：调用 extract_archive_members(member_id)
              ↓
建立日志时间线并验证 evidence incident window
```

## TODO 1：短期 Skill 指引

- [ ] 不要把 MTK/APLog 命名约定作为所有 Android 日志的无条件规则写入
      `android-log-triage`。
- [ ] 在 `android-log-triage` 中只增加识别和路由规则：检测到
      `APLog_YYYY_MMDD_HHMMSS__NN` 时，使用 MTK/APLog 专项选择规则。
- [ ] 在 `skills/mtk-ivi-log-analysis/references/` 新增或扩展 APLog 归档选择文档，并从
      `skills/mtk-ivi-log-analysis/SKILL.md` 链接。
- [ ] 明确事故时间优先来自已验证 Jira 上下文；如果只有模糊时间或没有日期，必须记录
      limitation，不能猜测 Boot 轮次或跨天日期。
- [ ] 明确按“事故时间的前驱归档 + 前后相邻归档”选择，不使用固定 1–2 分钟覆盖假设。
- [ ] 当工具尚不支持时间过滤时，不允许指导模型按裸路径调用
      `extract_archive_members`，因为当前契约只接受 `member_id`。

短期 Skill 只能减少错误推理，无法解决 `member_id` 已被工具结果截掉的问题；真正修复依赖
TODO 2。

## TODO 2：给 inspect_archive 增加时间过滤

### 输入契约

修改 `packages/log-analyzer-mcp/src/log_analyzer/domain.py` 中的
`InspectArchiveInput`。建议新增结构化字段，而不是自由文本：

```python
class ArchiveTimeRange(BaseModel):
    start: str  # YYYY-MM-DDTHH:MM:SS，本地 Case 时间，不隐式换算时区
    end: str    # 必须 >= start

class InspectArchiveInput(BaseModel):
    case_id: str
    artifact_id: str
    member_offset: int = 0
    source_sha256: str | None = None
    max_members: int = 1000
    time_range: ArchiveTimeRange | None = None
    neighbor_count: int = Field(default=1, ge=0, le=3)
    member_path_contains: str | None = None
```

注意：最终字段命名可以调整，但需要保持 MCP Schema 清晰。`time_range` 未提供时必须维持
现有行为，避免破坏旧调用方。

### 确定性解析

- [ ] 在 `archive_selection.py` 增加纯函数解析 APLog basename：

```text
APLog_YYYY_MMDD_HHMMSS__NN
```

- [ ] 只解析完整匹配的 basename；嵌套目录前缀不能影响解析。
- [ ] 校验日期、时间合法性，非法名称作为普通成员保留但不能参与时间过滤。
- [ ] 返回解析后的本地 naive datetime 和轮次编号；不要擅自附加时区。
- [ ] 对匹配成员按 `parsed_start_time` 排序，不依赖 ZIP/TAR 内部原始顺序。

### 时间范围选择语义

建议定义为：

1. 选择起始时间位于 `[time_range.start, time_range.end]` 的成员；
2. 加入 `start` 之前最近的 `neighbor_count` 个前驱成员；
3. 加入 `end` 之后最近的 `neighbor_count` 个后继成员；
4. 如果范围内没有成员，至少返回事故时间之前最近的前驱和之后最近的后继；
5. 去重后按起始时间排序；
6. 最后再应用 `member_offset/max_members`；
7. 非 APLog 成员只有在未指定 `time_range` 时按原行为返回。

如果产品实际希望“给一个事故点而不是范围”，可额外提供 `incident_time`，但不要让模型自己
根据 `__NN` 计算 offset。

### 输出契约

`inspect_archive` 应继续返回安全 `member_id`，并给时间选择增加可审计信息：

```json
{
  "selection": {
    "mode": "archive_name_time",
    "requested_start": "2026-08-31T06:15:00",
    "requested_end": "2026-08-31T06:30:00",
    "neighbor_count": 1,
    "matched_member_count": 2,
    "returned_member_count": 4
  },
  "members": [{
    "member_id": "stable-id",
    "member_path": "APLog_2026_0831_061532__79.tar.gz",
    "parsed_start_time": "2026-08-31T06:15:32",
    "time_relation": "predecessor | in_range | successor",
    "extracted": true,
    "artifact_id": "artifact-id-if-reusable"
  }]
}
```

- [ ] 时间过滤必须在序列化完整成员列表之前发生，确保结果远小于 40,000 字符。
- [ ] 保留当前 `source_fingerprint` 和翻页 SHA 校验。
- [ ] 保留当前跨分析解压复用字段：`extracted`、`artifact_id`、`relative_path`。
- [ ] 不要允许调用方用 `member_path` 绕过稳定 member ID 和路径安全校验。

## TODO 3：更新 Agent Prompt

- [ ] Jira/Local 工作流应先从已验证上下文得到 `reported incident time`。
- [ ] 遇到 APLog 命名时优先调用带 `time_range` 的 `inspect_archive`，不要逐页浏览全部条目。
- [ ] 若返回 `extracted=true + artifact_id`，直接复用，不再调用解压工具。
- [ ] 只把上报时间作为日志选择线索；真正的事故窗口仍需通过 ANR、Crash、Watchdog、
      SurfaceFlinger/HWC、重启标记等日志证据验证。
- [ ] 时间缺失或日期/Boot 轮次不明确时返回明确 limitation，不执行无边界的全量关键词扫描。

## TODO 4：测试

至少覆盖：

- [ ] 44 个 APLog 成员时，`06:15–06:30` 只返回范围内成员及前后邻居，结果不截断。
- [ ] `06:24:10` 能选出前驱 `061532__79` 和后继 `062429__80`，不依赖固定卷时长。
- [ ] 时间范围内没有成员时仍返回最近的前驱/后继。
- [ ] 跨午夜、不同日期和非法日期名称。
- [ ] ZIP/TAR 原始成员顺序混乱时仍按解析时间排序。
- [ ] 非 APLog 成员在无 `time_range` 时保持旧行为；有时间过滤时不误匹配。
- [ ] `source_sha256` 不匹配仍返回 `ARCHIVE_SOURCE_CHANGED`。
- [ ] 过滤结果继续提供可用于 `extract_archive_members` 的稳定 `member_id`。
- [ ] 已解压成员在新的 MCP 进程中直接返回 `artifact_id`，无需调用解压工具。
- [ ] 工具结果字符数低于 Agent Harness 上限，不出现 `tool_result_truncated=true`。
- [ ] 现有归档安全、压缩炸弹、路径穿越和累计预算测试全部保持通过。

建议运行：

```powershell
uv run pytest -q packages/log-analyzer-mcp/tests
uv run pytest -q tests/test_worker.py tests/test_agent.py tests/test_skills.py
```

如果本机默认 uv cache 不可用，可创建仓库内临时缓存并使用
`uv --cache-dir .uv-cache run ...`；测试后只删除已确认位于仓库内的该临时目录。

## 完成标准

1. Agent 给出事故时间窗口后，一次 `inspect_archive` 最多返回少量相关 APLog，而不是 44 条
   完整目录或多轮盲目分页。
2. 返回结果始终包含安全稳定的 `member_id`；已解压成员同时包含可直接复用的
   `artifact_id`。
3. 续分析不重复写出未变化的日志成员。
4. APLog 起始时间只是归档选择依据，不被错误当成事故发生证据。
5. 没有可靠事故日期、Boot 身份或跨时钟锚点时，系统显式报告证据不足。
6. 所有相关测试通过，`git diff --check` 无错误。

## 非本次范围

- 不实现任意归档命名规则的通用自然语言解析。
- 不允许模型提供裸文件路径进行解压。
- 不用 APLog 编号推断 Boot 轮次。
- 不把 Android wall clock、kernel monotonic、MCU/SCP counter 自动混为同一时间线。
- 不在本次实现完整的 `IncidentWindow` 持久化；可在后续 Investigation Checkpoint 中处理。
