# 真实稳定性黄金 Case Eval

黄金 Case 不是从模型答案自动生成的“自我评分集”。它来自一次真实运行的 Run Bundle，
经 Android 稳定性工程师复核后，以独立旁路记录保存；原始 Bundle 始终不修改。

## 反馈与晋升流程

1. 选择 `<case>/.bug-agent/runs/` 下已经完成且完整性校验通过的 Run Bundle v2。
2. 对五个稳定性维度按 0..4 评分，并选择 `accepted`、`corrected` 或 `rejected`。
3. `corrected` 必须填写正确的期望结论；可同时声明必须出现和禁止出现的主张。
4. Review 追加写入 `.bug-agent/reviews/<run>/`，并引用原 Bundle 的 SHA-256。
5. 只有 `accepted` 和 `corrected` 能晋升；黄金版本追加写入
   `.bug-agent/evals/golden/<golden_case_id>/`，不会覆盖历史复核版本。

评分维度不是文风偏好：

| 维度 | 关注点 |
|---|---|
| `incident_identity` | 是否锁定正确复现、boot、进程、build 和时间窗，避免跨事故拼接 |
| `evidence_grounding` | 主张是否引用本轮真实 Evidence，位置和摘录能否复核 |
| `causal_correctness` | 是否区分症状、失效机制和根因，是否把级联结果误判为根因 |
| `android_stability_coverage` | 是否覆盖该 Case 必要的 framework/native/kernel/watchdog/资源压力等层次 |
| `actionability` | 下一步是否能产生明确证据并证伪候选，而不是泛化建议 |

统一分值：`0` 缺失或有害，`1` 明显不足，`2` 部分正确，`3` 工程可用，`4` 可作为
黄金标准。`accepted` 要求五项都至少为 3；低于该门槛时必须选择 `corrected` 或
`rejected`，防止 verdict 与维度评分自相矛盾。

## Review JSON

先把 Run Bundle 生成本地复盘页。页面将执行过程、主张、Evidence、输入覆盖和复现
身份放在一起，并可直接导出下面的 Review JSON：

```powershell
bug-agent visualize-run <run-bundle.json>
```

```json
{
  "reviewer": "android-stability-team",
  "verdict": "corrected",
  "scores": {
    "incident_identity": 4,
    "evidence_grounding": 4,
    "causal_correctness": 3,
    "android_stability_coverage": 3,
    "actionability": 4
  },
  "labels": ["watchdog", "system_server"],
  "notes": "原结论把 watchdog kill 当成根因，实际根因是 binder 线程池耗尽。",
  "expectation": {
    "conclusion_status": "confirmed",
    "root_cause": "system_server binder 线程池耗尽导致 watchdog 超时",
    "evidence_ids": ["ev-12", "ev-18"],
    "required_claims": ["watchdog 是恢复动作而不是最初根因"],
    "forbidden_claims": ["低内存直接导致 system_server 被杀"],
    "required_missing_evidence": []
  }
}
```

保存反馈并晋升：

```powershell
bug-agent eval-review <run-bundle.json> <review.json> --promote
```

对新的 Run Bundle 执行确定性回归：

```powershell
bug-agent eval-run <candidate-run-bundle.json> <golden-case.json>
```

当前 Runner 不调用 LLM 自我打分。它检查输入指纹、Evidence grounding、结论等级、
根因文本、根因 Evidence、事故身份、必须/禁止主张和必须声明的证据缺口。结果追加保存
到 `.bug-agent/evals/results/`。文本主张采用规范化后的精确/包含关系匹配，因此结果稳定、
可解释；未来若增加语义裁判，也必须作为独立的非确定性指标，不能覆盖这些硬门禁。

金标准还可以约束 Agent 控制面：`required_tools`、`forbidden_tools`、
`required_skills`、`forbidden_skills`、`max_tool_calls` 和 `max_human_checkpoints`。
默认 `max_human_checkpoints=0`，目标是让已经被人工提示解决过的问题在后续版本中自动
完成；确实依赖现场信息、无法从 Case 推断的问题可以显式放宽。Eval 不要求完全一致的
调用顺序，只检查必要调查能力、禁止行为、预算和人工依赖。

## 从人工提示进入 Agent 优化

交互会话消费人工检查点后，会在
`<case>/.bug-agent/optimization/candidates/<checkpoint_id>.json` 自动生成完整性密封的
待审核候选。候选保留阻塞原因、人工回复、回复后真实发生的工具调用与 Skill 激活，并
初步归因到 `tool_selection`、`skill_routing`、`skill_content` 或 `case_context`。

候选不是可直接合入的模型建议，也不会自动改写 Skill。评审者需要确认归因，选择对应的
工具策略、Skill 或输入契约改动，再用同类黄金 Case 验证：人工检查点减少，必要 Tool/Skill
仍被调用，Evidence grounding 与事故身份门禁保持通过。原始人工回复会保存在本地候选中，
应与日志和会话记录采用相同的访问控制与保留周期。

`accepted` 可省略 `expectation`，系统会把已经通过人工确认的结构化 RCA 固化为期望；
`rejected` 只保留负反馈，不允许晋升。任何被修改或与 Review 不匹配的 Bundle 都会被拒绝。
`accepted` 还必须已经通过本轮 Evidence grounding 校验。`confirmed` 金标准必须同时
具备根因 Evidence 和事故身份锚点；Review 引用的 Evidence ID 必须真实存在于该 Bundle
的 Evidence Registry，避免人工标注再次引入无法回查的“口头真值”。

黄金条目保存输入指纹、基线 provenance、期望主张和金标准 Evidence 引用，不复制原始
日志。后续 Eval Runner 应先验证输入内容哈希覆盖；覆盖不完整的 Case 可用于质量回归，
但不能宣称是完全相同输入下的确定性重放。
