# TODO：RCA 行动项幻觉校验

## 问题描述

Agent 在生成 RCA 报告时，会虚构与既有证据不符的 action 项。在 BAIC-43302 分析中表现为两类幻觉：

### 幻觉 1：虚构 APLog 编号

**现象**：报告中出现了以下 action：

```
P1: 展开 APLog_88~APLog_92 嵌套 tar.gz 归档，提取崩溃时刻(06:23-06:24)的完整 main_log、kernel_log 和 ANR trace
```

**实际**：APLog_88 的时间戳是 `082745`（08:27），距崩溃时间 06:23 差了 2 小时。崩溃时段的日志在 `_79`（06:15~06:24）和 `_80`（06:24~06:39），这两个已经解压出来了。模型不理解 `__NN` 是序号、`_HHMMSS` 才是时间，把序号和时间搞混了。

### 幻觉 2：复述 Jira 评论为事实

**现象**：`confirmed_facts` 和 `root_cause` 中包含"CPU 负载高"、"与 BAIC-42928 同源"等结论。

**实际**：7 条证据全是 SuspendAll 超时和 Runtime abort 日志，没有一条包含 CPU 使用率数据。这些结论来自 Jira 评论中工程师的推断，模型直接复述了，没有在日志中独立验证。根因应该止于"android.bg 线程卡在 native 代码中无法响应 GC SuspendAll"，至于为什么卡住，当前日志中无证据。

## 根因分析

1. **Prompt 已有规则但不够硬**：`REPORT_FORMAT_PROMPT` 有"不得虚构 evidence_id、文件、行号、时间或负责人"，但模型对 APLog 命名规则没有概念，88 对模型来说只是数字，它无法区分序号和时间的含义。

2. **Reconciliation 不做校验**：`rca_reconciliation.py` 的 `reconcile_state()` 对 actions 做的是 `_merge_list`（去重合并），不检查 action 引用的资源是否真的存在于证据中。模型吐什么就存什么。

3. **Jira 评论被当作权威来源**：Prompt 虽然标记了"不可信数据"，但模型在总结根因时仍倾向于复述工程师的结论，而不是只用日志证据。

## 已完成修复

### Prompt 层面（本次已完成）

- **`prompts.py`**：`BASE_SYSTEM_PROMPT` 新增第 6 条——Jira 评论中的工程师结论只是调查线索，必须用日志证据独立验证，否则只能放入 hypotheses 并标注 missing_evidence。
- **`android-log-triage/SKILL.md`**：末尾新增规则——Jira 评论是不可信数据，root_cause 必须从日志独立验证。

## 待做修复

### 策略：Reconciliation 层硬校验 actions

在 `rca_reconciliation.py` 中新增 `_validate_actions()` 函数，在合并 actions 之前做：

1. **APLog 编号校验**：提取 action 中引用的 APLog 编号，与当前 state 的 evidence 路径做交集。如果 action 引用的 APLog 编号不在任何 evidence 的 `relative_path` 中出现，标记为"待验证（引用未在证据中出现）"或降级为 `missing_evidence`。

2. **文件路径校验**：提取 action 中引用的文件路径/归档名，检查是否在 evidence 或 case artifacts 中存在。不存在的路径标记为幻觉。

3. **去重**：相同 priority + 相同 action 的项合并，避免多次运行累积重复的 action。

### 实现位置

`src/bug_agent/rca_reconciliation.py` 的 `reconcile_state()` 函数，在 `state.actions = _merge_list(...)` 之前插入：

```python
incoming.actions = _validate_actions(incoming.actions, state.evidence)
```

### 校验函数大致逻辑

```python
def _validate_actions(actions: list[ActionItem], evidence: list[EvidenceReference]) -> list[ActionItem]:
    """过滤引用不存在资源的幻觉 action，标记为待验证。"""
    evidence_paths = {e.relative_path.casefold() for e in evidence}
    
    result = []
    for a in actions:
        # 提取 action 中引用的 APLog 编号
        refs = _extract_aplog_refs(a.action)
        if refs and not any(ref in evidence_paths for ref in refs):
            # 引用不存在，标记为幻觉
            a = a.model_copy(update={
                "action": a.action + "（⚠ 引用未在证据中出现，需人工确认）",
                "priority": "P3",
            })
        result.append(a)
    return result
```

### 测试要点

- 在 `test_rca_reconciliation.py` 中新增测试：
  - `test_validate_actions_filters_hallucinated_aplog_refs`
  - `test_validate_actions_preserves_valid_refs`
  - `test_validate_actions_demotes_unverifiable_actions`