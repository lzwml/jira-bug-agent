# 自适应 Bug 调查策略与评估计划

状态：Proposed  
目标读者：后续实现 Agent、Skill 与 Eval 的开发者  
前置检查点：`e3f1b34`、`c720438`

## 1. 背景与问题

BAIC-46391 暴露了两类不同问题：

1. 工具结果过大且缺少结构关系，导致关键 Artifact 容易被截断或遗漏；
2. Agent 会被工具列表顺序、静态“高价值”标签和首个假设锚定，进而影响后续全部工具调用与最终 RCA。

前置提交已经解决确定性基础问题：

- `inspect_case` 支持类型/路径过滤、分页和有界摘要；
- APLog 会显式返回 active `.curf` 与 immediate rotated predecessor 的连续流关系；
- 工具返回采用紧凑 JSON，降低无效字符开销；
- 报告合成阶段使用完整报告 Schema 和本轮真实 Evidence Registry。

本计划不再为 Artifact 设置全局静态优先级。AEE、pstore、ANR、音频、视频等证据的价值必须相对于当前事故画像、候选假设、时间窗和待补覆盖来判断。

## 2. 核心设计原则

### 2.1 职责边界

| 层 | 职责 | 禁止承担的职责 |
| --- | --- | --- |
| Log/Core/MCP | 返回 Artifact 客观属性、结构关系、内容时间、解析结果 | 判断某附件是否是当前 Bug 的根因证据 |
| Skill | 定义某类问题怎么调查、检查哪些边界、何时证据充分 | 文件系统操作、权限扩张、对自身效果打分 |
| Agent Planner | 根据事故画像组合 Skill、维护假设和选择下一项调查 | 把默认排序当作事实或因果 |
| Validator/Eval | 独立检查覆盖、证据、因果和预算 | 依赖模型自报“已完成”作为唯一依据 |

### 2.2 两类规则必须分开

确定性正确性约束可以是硬规则：

- 未检查 active `.curf` 与前驱轮转文件，不得声称该流存在日志缺口；
- 重启/跨 boot 问题必须区分事故前后 boot identity；
- 时间相邻、同名组件和单条 warning 不能单独建立因果；
- confirmed root cause 必须引用本轮 Evidence Registry 中的证据；
- 缺失必需域或时间覆盖时必须降级结论。

调查相关性只能是上下文规则：

- AEE 只在 Crash、panic、watchdog、重启等路线中通常相关；
- AudioFlinger/DSP 只在音频链路假设中通常相关；
- SurfaceFlinger/WindowManager/视频只在显示和交互假设中通常相关；
- 任何推荐都必须说明它验证哪个假设、覆盖哪个缺口以及预计成本。

### 2.3 不使用全局单一“价值分数”

MVP 不实现跨问题域统一的 `priority_score`。单一数字会制造不可比较的伪精度，并把新的静态偏置隐藏到公式中。

候选选择使用多维理由：

- `symptom_relevance`：与上报症状或目标组件的关系；
- `identity_relevance`：是否帮助锁定时间、boot、进程、VM 或操作；
- `hypothesis_test`：支持或证伪哪个候选假设；
- `coverage_gain`：补齐哪个尚未检查的域/日志流；
- `information_gain`：结果是否能区分多个竞争性解释；
- `cost`：预计读取量、解压量和工具调用数；
- `limitations`：时钟不可信、内容截断、缺少目标进程等限制。

Planner 应选择能以合理成本最大幅度减少关键不确定性的候选，而不是机械取 Top-K。

## 3. 目标运行流程

```text
Jira/Local Case
    ↓
确定性附件普查（inspect_case）
    ↓
建立 IncidentProfile
    ↓
选择一个 primary symptom Skill
可选一个 secondary symptom Skill + platform/supporting Skills
    ↓
建立 hypotheses + coverage ledger + next candidates
    ↓
执行一个有理由的工具动作
    ↓
用新 Evidence 更新假设、覆盖和候选
    ↺ 直到满足结束条件或预算耗尽
    ↓
独立 pre-report gate
    ↓
报告合成 + Evidence 校验
```

不得在 `inspect_case` 后直接进入无计划的宽泛关键词搜索，也不得因为某类附件出现在返回前部就默认选择它。

## 4. 运行时数据契约

以下结构应进入稳定 Run Bundle，便于复盘和 Eval。字段名可以在实现时微调，但语义不得退化成自由文本。

### 4.1 IncidentProfile

```json
{
  "symptom_family": "reboot | native_crash | anr_freeze | display | audio | network | ota | can_mcu | virtualization | unknown",
  "reported_window": {"start": null, "end": null, "clock_domain": "reported"},
  "target_components": [],
  "target_processes": [],
  "reboot_suspected": false,
  "user_visible_symptom": "",
  "trigger_context": "",
  "source_refs": [],
  "uncertainties": []
}
```

要求：

- `source_refs` 必须指向 Jira 描述、comment_id、用户输入或已验证 Evidence；
- 未知字段保持空值并进入 `uncertainties`，禁止模型补全；
- `symptom_family=unknown` 时继续使用通用 triage，不得强选专项 Skill。

### 4.2 Hypothesis

```json
{
  "hypothesis_id": "hyp-1",
  "claim": "",
  "role": "primary | competing | downstream",
  "status": "open | supported | contradicted | confirmed | blocked",
  "supporting_evidence_ids": [],
  "contradicting_evidence_ids": [],
  "required_observations": [],
  "missing_evidence": [],
  "next_falsification": ""
}
```

要求：

- 第一个解释只能是 `open`，不能在未调查时直接成为 `confirmed`；
- 至少保留一个竞争性解释，除非已有确定性诊断直接排除其他机制；
- `confirmed` 必须同时具备直接机制证据和事故身份锚点。

### 4.3 CoverageLedger

```json
{
  "coverage_id": "android-main-incident-boot",
  "domain": "android | linux | hypervisor | mcu | can | video | code",
  "stream": "main | system | events | crash | kernel | anr | tombstone | ...",
  "boot_identity": null,
  "target_window": null,
  "status": "planned | checked | missing | unavailable | not_applicable",
  "artifact_ids": [],
  "evidence_ids": [],
  "limitations": []
}
```

`checked` 必须来自实际工具轨迹，不能只由模型声明。Validator 应使用 build/search/timeline/diagnostic 事件反向验证。

### 4.4 InvestigationCandidate

```json
{
  "candidate_id": "cand-1",
  "artifact_ids": [],
  "query_or_action": "",
  "tests_hypothesis_ids": [],
  "fills_coverage_ids": [],
  "selection_reasons": {
    "symptom_relevance": "",
    "identity_relevance": "",
    "information_gain": "",
    "cost": ""
  },
  "limitations": []
}
```

`InvestigationCandidate` 由 Planner 生成，MCP 只提供生成它所需的客观 Artifact 描述。

## 5. Skill 体系完善

### 5.1 保留现有组合模型

- 默认基础 Skill：`android-log-triage`；
- 一个 primary symptom Skill；
- 最多一个 secondary symptom Skill，仅用于已观察到的级联影响；
- platform Skill：如 `mtk-ivi-log-analysis`；
- supporting Skill：如 `aee-db-extract`、`code-search`。

### 5.2 每个 symptom Skill 应具备的固定章节

1. `Activation`：适用信号、反向信号和 unknown 时的处理；
2. `Incident identity`：必须锁定的时间、boot、进程或操作身份；
3. `Initial hypotheses`：候选机制模板，不是默认根因；
4. `Required coverage`：按条件展开的必查域和日志流；
5. `Evidence semantics`：什么只能算线索，什么可以证明直接机制；
6. `Competing explanations`：至少列出常见替代机制及证伪方式；
7. `Tool strategy`：优先使用的有界查询方式和成本控制；
8. `Stopping conditions`：confirmed、hypothesis_only、insufficient_evidence 的条件；
9. `Failure patterns`：该路线常见的误判和禁止推断。

平台 Skill 只描述日志拓扑、时钟、归档和跨域语义，不替 symptom Skill 决定根因路线。能力 Skill 只描述工具正确用法。

### 5.3 Skill 的机器可验证元数据

第一阶段不要把整份自然语言 Skill 编译成复杂规则引擎。仅为 Eval 增加少量稳定元数据：

```yaml
category: symptom
symptom_family: reboot
required_coverage_contract: reboot-v1
```

`required_coverage_contract` 指向代码维护的版本化 Validator 契约。自然语言 Skill 负责方法，Validator 契约负责最低质量门禁，避免两份大规模规则重复。

## 6. Agent 与 Runtime 改造

### 6.1 增加受控的调查状态工具

建议新增本地工具 `update_investigation_state`，只更新内存中的结构化状态，不访问外部系统。至少支持：

- 首次写入 `incident_profile`；
- 添加/更新 hypothesis；
- 计划 coverage；
- 记录下一候选及选择理由；
- 将工具结果关联到 coverage/hypothesis。

Runtime 校验 ID、状态迁移和 Evidence 引用。模型不能把不存在的 Evidence 写入状态。

### 6.2 最低运行约束

- `inspect_case` 后、首次大规模 build/search 前，应存在 IncidentProfile 和 primary/unknown 路线；
- 输出根因前必须存在 coverage ledger；
- `confirmed` 前必须完成对应 symptom Skill 的 required coverage contract；
- 预算将耗尽时，优先保存结构化缺口并降级结论，而不是补写未经调查的确定性描述；
- 不强制完全一致的工具顺序，允许模型根据新证据调整路线。

初期可通过 feature flag 启用：

```text
BUG_AGENT_ADAPTIVE_INVESTIGATION=false | shadow | enforce
```

- `false`：保持现有行为；
- `shadow`：记录状态和违规，但不阻止报告；
- `enforce`：关键门禁失败时降级结论。

### 6.3 Artifact 发现接口

继续保持 MCP 中立：

- `inspect_case` 返回 kinds、分页信息、结构关系和能力要求；
- 后续可增加客观字段：可解析性、容器层级、内容时间范围、boot hint、process hint；
- 不新增 `high_value`、`root_cause_likelihood` 或全局 `priority_score`；
- 若需要候选检索，接口参数应来自 IncidentProfile，例如 kind/path/time/boot/domain，而不是由 MCP 猜测问题类型。

## 7. 独立 Eval 设计

### 7.1 在现有五维评分上增加过程指标

保留 `docs/golden-case-eval.md` 的五项最终质量指标，并新增：

| 指标 | 含义 |
| --- | --- |
| `incident_profile_accuracy` | 症状类型、目标组件、时间和 boot 是否忠于输入 |
| `required_coverage_recall` | Skill 条件要求的覆盖项是否实际检查 |
| `premature_conclusion` | 是否在关键覆盖完成前形成 confirmed 结论 |
| `competing_hypothesis_coverage` | 是否验证至少一个合理替代解释 |
| `first_useful_action` | 前几次工具调用是否能锁定身份或区分假设 |
| `irrelevant_tool_ratio` | 与任何 hypothesis/coverage 都无关联的调用比例 |
| `duplicate_tool_ratio` | 无新信息的重复调用比例 |
| `budget_compliance` | 是否在步数、调用数和时间预算内完成 |
| `run_consistency` | 相同输入多次运行的事故身份和核心结论是否稳定 |

### 7.2 硬门禁与软指标

硬门禁必须确定性执行：

- Evidence ID、Artifact、路径、行号有效；
- required/forbidden claims；
- 必需 boot/stream 覆盖；
- `.curf` 连续性；
- confirmed 的事故身份和根因证据；
- 禁止工具、Skill 冲突和预算上限。

软指标用于比较版本：

- 工具效率；
- 候选假设质量；
- 行动顺序合理性；
- 报告清晰度。

LLM Judge 如后续引入，只能作为附加软指标，不能覆盖硬门禁。

### 7.3 Case 矩阵

初始 Eval 至少覆盖：

- reboot/panic/watchdog；
- native crash；
- ANR/UI freeze；
- black screen/display；
- audio；
- network/connectivity；
- virtualization/cross-domain；
- 无法确认根因的 insufficient-evidence Case；
- 包含误导性 Jira 评论或无关 AEE 的 Case。

每一类应同时包含：正例、容易被错误线索锚定的反例、关键证据缺失例。BAIC-46391 应作为“APLog 连续性 + 错误因果 + 跨域覆盖”回归 Case，而不是唯一设计依据。

### 7.4 首轮验收标准

候选实现进入 `enforce` 前必须满足：

1. 现有 Golden Case 硬门禁无回退；
2. 所有 confirmed 结论 Evidence grounding 通过率为 100%；
3. 所有标注的 required coverage 均由真实工具事件验证；
4. BAIC-46391 不再产生 00:16:49 后日志缺口的错误判断；
5. 非重启 Case 不因存在 AEE/pstore 而自动选择重启路线；
6. 缺失关键域的 Case 必须降级，不得用邻近 warning 补足因果；
7. 平均工具调用数相对基线的增长需要有覆盖收益解释，且不得突破现有硬预算；
8. 同一模型配置重复运行时，事故身份与结论等级保持稳定。

## 8. 分阶段实施

### Phase 0：基线冻结（已完成）

- 提交确定性 Artifact 发现与 APLog 连续性修复；
- 提交报告 Evidence Registry 修复；
- 保存 BAIC-46391 原始报告、corrected 报告和 Run Bundle 作为复盘输入。

### Phase 1：可观察性与契约

- 在 `contracts.py` 增加 IncidentProfile、Hypothesis、Coverage、Candidate 模型；
- 将调查状态写入 Run Bundle，但不改变最终报告 Schema；
- 增加状态序列化、Evidence 引用和状态迁移测试；
- feature flag 默认 `shadow`。

### Phase 2：Skill 标准化

- 按固定章节审查现有 symptom Skills；
- 删除平台 Skill 中重复的症状决策；
- 为 symptom Skill 绑定版本化 required coverage contract；
- 增加 Skill 激活正例、反例和冲突测试。

### Phase 3：Planner 闭环

- 增加 `update_investigation_state`；
- Prompt 要求每个工具动作关联 hypothesis 或 coverage；
- 工具结果后允许更新、证伪或替换 primary hypothesis；
- pre-report gate 检查状态与真实工具轨迹一致性。

### Phase 4：Eval 扩展

- 扩展 Golden Case JSON 以保存 incident profile 和 coverage 期望；
- Eval Runner 增加过程硬门禁和效率指标；
- 建立多症状 Case 矩阵并记录当前基线；
- 对候选版本执行同模型、同配置、同输入的重复运行。

### Phase 5：渐进启用

- `shadow` 收集违规和成本数据；
- 先对 reboot/native-crash 路线启用 `enforce`；
- 通过对应 Case 矩阵后逐类扩大；
- 任何路线回退时只关闭该路线，不回滚确定性工具能力。

## 9. 预计修改位置

- `src/bug_agent/contracts.py`：调查状态数据模型；
- `src/bug_agent/agent.py`：状态工具、运行门禁；
- `src/bug_agent/worker.py`：Run Bundle 持久化和 pre-report gate；
- `src/bug_agent/prompts.py`：Planner 协议，减少领域硬编码；
- `src/bug_agent/skill_router.py`：激活依据与 coverage contract；
- `src/bug_agent/skills.py`：Skill 元数据解析；
- `src/bug_agent/run_bundle.py`：调查状态和版本字段；
- `src/bug_agent/eval_*`：Golden Case 过程评估；
- `skills/*/SKILL.md`：按标准章节收敛；
- `packages/log-analyzer-mcp`：仅补充客观 Artifact 元数据，不做因果排序。

## 10. 明确非目标

- 不在 MCP 中实现通用根因推荐器；
- 不用另一模型替代确定性 Evidence 校验；
- 不要求所有问题采用相同日志流或相同工具顺序；
- 不一次性重写全部 Skills；
- 不自动把人工反馈直接写回 Skill；
- 不因本计划扩大 Jira 写权限或 Case 文件访问范围；
- 不以提高 token/工具调用量作为质量提升的替代指标。

## 11. 后续模型接手清单

1. 先阅读本文件、`docs/architecture.md`、`docs/golden-case-eval.md`；
2. 检查前置提交 `e3f1b34` 和 `c720438`，不要恢复静态 `priority_artifacts`；
3. 从 Phase 1 开始，只实现数据契约和 shadow 记录；
4. 每个 Phase 单独提交并运行完整测试；
5. 未建立多症状 Eval 基线前，不启用 `enforce`；
6. 所有新“推荐”字段必须回答：相对于哪个症状、假设、身份或覆盖缺口；
7. 若设计需要跨问题域单一分数，先提供反例验证，不得直接落地；
8. 最终在文档中记录实际指标、失败 Case 和尚未覆盖的症状路线。
