# RCA 迭代与冲突协调 交接文档

更新时间：2026-08-31

## 背景

当前 `jira-bug-agent` 每次运行都会生成一份独立的 RCA Markdown 文件（`runs/<task-id>.md`）。多次运行同一个 Case 会产生多份可能不一致的报告，缺乏统一的"当前结论"视图。

### 当前产出

```text
<Case>/.bug-agent/
├── rca-state.json           # 需要新增：当前唯一结构化 RCA
├── RCA.md                   # 需要新增：当前唯一人类可读 RCA
├── rca-events.jsonl         # 需要新增：RCA 变更审计日志
├── index/
│   └── logs.sqlite3
└── runs/
    ├── <task-id-1>.json     # 已有：每次执行轨迹
    ├── <task-id-2>.json
    └── <task-id-1>.md       # 需要停止生成：不再产生多份 MD
```

### 怎么停止生成多份 MD

当前 `write_rca_report` 在 `runstore.py` 里，每次运行写一份 `runs/<task-id>.md`。改为：只写 `runs/<task-id>.json`（执行轨迹），不再写 per-run MD。RCA 的 MD 渲染由 Reconciliation 阶段统一写入 `RCA.md`。

## 目标

1. 同一个 Case 只有一份正式 RCA 文件（`.bug-agent/RCA.md`）
2. 新分析结果不会盲目覆盖旧结论，冲突需要显式标记
3. 每次结论变更都有审计记录（`rca-events.jsonl`）
4. 历史执行轨迹保留在 `runs/*.json`，不丢失

## 数据模型

### 1. RCA State（`rca-state.json`）

唯一的当前结构化 RCA 状态，是 `RCA.md` 的数据源。

```typescript
interface RCAState {
  revision: number;           // 单调递增，每次更新 +1
  updated_at: string;         // ISO 8601
  based_on_runs: string[];    // 参与当前结论的 run task_id 列表
  conclusion_status: "confirmed" | "hypothesis_only" | "insufficient_evidence";
  summary: string;
  observed_symptom: string | null;
  failure_mechanism: string | null;
  root_cause: string | null;
  trigger_conditions: string[];
  timeline: TimelineEntry[];
  coverage: CoverageItem[];
  confirmed_facts: string[];
  claims: Claim[];            // 原子主张，每条有独立状态
  negative_findings: NegativeFinding[];
  missing_evidence: string[];
  actions: ActionItem[];
  evidence: EvidenceReference[];
  metadata: { task_id: string; steps: number; skills: string[]; } | null;
}
```

### 2. Claim（原子主张）

把结论拆成独立 Claim，不是一段自然语言。

```typescript
interface Claim {
  claim_id: string;                          // 稳定 ID，如 "claim-phud-anr"
  type: "observed_symptom" | "failure_mechanism" | "root_cause";
  statement: string;
  status: "candidate" | "supported" | "confirmed" | "disputed" | "rejected" | "superseded";
  confidence: "high" | "medium_high" | "medium" | "low";
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  missing_evidence: string[];
  falsification: string | null;
  created_run_id: string;                    // 首次提出该 Claim 的 run
  superseded_by: string | null;              // 被哪个 claim_id 替代
  replaced_by: string | null;                // 被哪个 claim_id 替换
}
```

### 3. RCA Event（`rca-events.jsonl`）

Append-only，每行一条 JSON：

```typescript
interface RCAEvent {
  revision: number;
  event: "claim_created" | "claim_updated" | "claim_status_changed" |
         "root_cause_changed" | "evidence_added" | "state_initialized";
  run_id: string;              // 触发本次变更的 run
  detail: {
    claim_id?: string;
    from_status?: string;
    to_status?: string;
    reason: string;
    evidence_ids?: string[];
  };
  timestamp: string;
}
```

### 4. 执行轨迹（`runs/<task-id>.json`）

已有，无需改动。每次运行结束后写入 `build_run_record` 的结果。

## Reconciliation 流程

每次 Agent 运行完成后，不直接覆盖 `RCA.md`，而是进入 Reconciliation 阶段。

### 输入

- 当前 `rca-state.json`（如果是首次运行，则为空）
- 本次 `BugAnalysisResult`（via `runs/<task-id>.json`）
- 本次新增 Evidence（从 `result.report.evidence` 提取）

### 步骤

1. **加载当前状态**：如果 `rca-state.json` 不存在，以本次结果初始化，直接写入
2. **提取 Claims**：从本次 `BugAnalysisResult` 中提取所有 Claim
3. **逐条比对**：对每条 Claim，判断与当前状态中已有 Claim 的关系
4. **更新状态**：按规则更新 Claim 状态，追加 RCA Event
5. **原子写入**：`rca-state.json` + `RCA.md` + `rca-events.jsonl`

### 比对规则

| 关系 | 判断条件 | 处理 |
|------|---------|------|
| **same** | 同一 type 和 component，语义等价 | 合并 evidence_ids，不改变状态 |
| **supports** | 新 Claim 增强了旧 Claim 的证据 | 保留旧 Claim，追加 evidence，可能提升 confidence |
| **extends** | 新 Claim 提供了旧 Claim 没有的新信息 | 新增 Claim，不修改旧 Claim |
| **contradicts** | 新 Claim 与旧 Claim 直接矛盾（如不同的 root_cause，或一个说 SF 正常另一个说 SF 故障） | 旧 Claim → `disputed`，新 Claim → `candidate`，整体 conclusion_status → `hypothesis_only` |
| **unrelated** | 不同 layer/component | 分别保留 |

### 冲突仲裁规则

- **confirmed 不可被自动覆盖**：已确认的 root_cause 不能由模型自动改为 `disputed` 或 `rejected`，必须人工操作
- **未覆盖不可自动 reject**：新运行没有覆盖旧 Claim 的证据范围时，不能将旧 Claim 标为 `rejected`
- **零匹配 ≠ 反证**：search_evidence 返回 0 结果仅在 `negative_findings` 中记录，不能作为 `contradicting_evidence_ids`
- **disputed 状态需人工裁决**：`disputed` 的 root_cause 应提示人工介入

### 初始化

首次运行，没有 `rca-state.json` 时：

1. 以本次 `BugAnalysisResult` 为初始状态
2. 所有 hypothesis 转为 Claim
3. `revision = 1`
4. 写入 `rca-state.json` 和 `RCA.md`
5. 追加 `state_initialized` 事件到 `rca-events.jsonl`

## 实现文件

### 需要新增

| 文件 | 职责 |
|------|------|
| `src/bug_agent/rca_state.py` | RCAState / Claim 数据模型 |
| `src/bug_agent/rca_reconciliation.py` | 比对逻辑、冲突仲裁、合并更新 |
| `src/bug_agent/rca_store.py` | 读写 `rca-state.json`、`rca-events.jsonl`、`RCA.md` |

### 需要修改

| 文件 | 改动 |
|------|------|
| `src/bug_agent/runstore.py` | `write_rca_report` 停止生成 `runs/<task-id>.md`，改为调用 Reconciliation |
| `src/bug_agent/worker.py` | 在 `execute()` 返回前调用 Reconciliation |
| `src/bug_agent/renderer.py` | 无改动（RCA.md 渲染复用现有 render_markdown） |

### 需要新增测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_rca_state.py` | Claim 状态机、RCAState 序列化 |
| `tests/test_rca_reconciliation.py` | 五种比对关系、冲突仲裁、confirmed 不可覆盖、未覆盖不可 reject |
| `tests/test_rca_store.py` | 原子写入、revision 递增、event 追加 |

## 与现有代码的集成点

1. `worker.py` 的 `execute()` 方法，在 `write_run_record` 之后、`return result` 之前，调用 Reconciliation：

```python
from .rca_reconciliation import reconcile

reconcile(task, result, run, run_config)
```

2. `runstore.py` 的 `write_rca_report` 不再生成 per-run MD，改为仅保存 JSON trace。

3. Reconciliation 写入失败时只 warning，不影响主流程。

## 安全与原子性

- `rca-state.json` 使用原子写入（先写 `.tmp`，再 `os.replace` 或 `Path.replace`）
- `rca-events.jsonl` 使用 append-only 写入，不修改已有行
- `RCA.md` 从 `rca-state.json` 渲染生成，每次全额覆盖（原子写入）
- 写入失败不抛异常，只 log warning

## 测试建议

优先覆盖：

1. **首次运行初始化**：`rca-state.json` 不存在 → 正确创建
2. **同向证据合并**：两次运行确认同一 Claim → evidence_ids 合并，revision 递增
3. **冲突标记**：两次运行 root_cause 矛盾 → 旧 Claim `disputed`，新 Claim `candidate`，整体 `hypothesis_only`
4. **confirmed 保护**：已有 `confirmed` root_cause → 新运行不能自动覆盖
5. **未覆盖不可 reject**：新运行没有查某个 layer → 旧 Claim 保持原状
6. **原子写入**：写入中途失败 → 旧文件完好

## 不在本次范围

- 人工审批流程（pending_review / approved_by）
- Jira 回写
- 自然语言语义匹配（先用 Claim type + component 做简单匹配）
- 跨 Case 的 Claim 复用