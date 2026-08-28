# log-analyzer-mcp

面向 Android Bug Agent 的证据驱动日志分析 MCP Server。

MCP package 负责 Case 文件权限、扫描预算和 Tool Contract；时间戳、稳定 ID、
诊断信号等确定性算法位于同一 workspace 的 `log-analysis-core`。问题类型的
调查顺序、关键词和时间线 anchors 位于仓库 `skills/`，不再硬编码在 Server。

V2 不再把任意目录路径直接交给每个工具。Agent 必须先注册一个 Bug Case，
后续通过 `case_id` 和 `artifact_id` 检索证据、提取时间线和解析诊断信息。

## 核心领域模型

```text
Case
└── Artifact
    ├── Evidence
    ├── TimelineEvent
    └── DiagnosticFinding
```

- **Case**：一次 Bug 分析的完整目录。
- **Artifact**：Case 中的 logcat、kernel、ANR、tombstone、SOS 等附件。
- **Evidence**：带文件、行号、上下文和稳定 ID 的原始证据。
- **TimelineEvent**：带时钟域的关键事件。
- **DiagnosticFinding**：AVC、Kernel Call Trace、Fatal、ANR 等结构化发现。

## MCP 工具

| 工具 | 作用 |
|---|---|
| `open_case` | 注册允许根目录内的案例目录，返回 Case 和 Artifact 清单 |
| `inspect_case` | 查看附件类型、大小和样本清单 |
| `inspect_archive` | 只读取归档成员清单和安全元数据，不把内容落盘 |
| `extract_archive_members` | 使用稳定成员 ID 选择性安全展开归档内容 |
| `build_index` | 按需为选中的文本日志建立持久化分块索引 |
| `prepare_case` | 全量安全展开归档并建立索引；仅作为显式批处理或兜底入口 |
| `search_evidence` | 字面量搜索日志，返回带前后文和行号的 Evidence |
| `extract_timeline` | 提取 Android、Wall Clock、Kernel monotonic 关键事件 |
| `parse_diagnostics` | 提取 AVC、Kernel Stack、Fatal 和 ANR |

搜索零匹配会返回成功结果：

```json
{
  "success": true,
  "data": {
    "items": [],
    "match_count": 0,
    "truncated": false
  }
}
```

这表示“未发现该证据”，不表示工具执行失败。

## 安全边界

允许访问的根目录只能由 MCP Server 启动配置指定，模型不能通过 Tool Call
扩大权限范围。

### Windows

```powershell
$env:LOG_ANALYZER_ALLOWED_ROOTS = 'D:\BugCases;D:\SharedLogs'
python -m log_analyzer.server
```

### Linux/macOS

```bash
export LOG_ANALYZER_ALLOWED_ROOTS=/data/bug-cases:/data/shared-logs
python -m log_analyzer.server
```

如果未配置，只允许 Server 的当前工作目录。

选择性展开和 `prepare_case` 都不修改原始附件。每个归档展开到它旁边的
`<归档文件名>.unpacked/`，缓存清单保存在该目录中；SQLite 索引写入
Case 下的 `.bug-agent/`。配置 `LOG_ANALYZER_WORK_ROOT` 后，可将索引和内部状态
集中写入指定的服务端工作区。

其他安全约束：

- 原始附件始终只读；归档旁只创建带受控 Manifest 的 `.unpacked` 目录；
- 已存在但没有有效 Manifest 的同名 `.unpacked` 目录不会被覆盖；
- Case 注册时跳过符号链接；
- 解析后的真实路径必须仍在允许根目录和 Case 目录内；
- 归档成员拒绝绝对路径、盘符、`..`、符号/硬链接、特殊文件和重复目标；
- 展开受单归档、单成员、总字节、文件数、压缩比、嵌套深度和运行时间预算限制；
- 内置支持 ZIP、TAR、TAR.GZ、TGZ 和单文件 GZIP；RAR/7z 明确报告不支持，
  不调用外部命令回退；
- 单文件和单次调用有扫描预算；
- 已准备的文本日志使用持久化分块索引；索引缺失或预算截断时安全退化为流式扫描；
- 搜索仅支持字面量，不接受模型提供的任意正则；
- 工具返回相对路径和稳定 ID，不向模型暴露内部绝对路径。

## 安装与启动

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
python -m log_analyzer.server
```

运行测试：

```bash
python -m pytest -q
```

也可以在没有 pytest 的环境运行 V2 标准库测试：

```bash
PYTHONPATH=src python -m unittest tests.test_service_v2 -v
```

完整仓库的 CLI 可以在不启动模型的情况下显式执行全量准备：

```powershell
uv run bug-agent prepare-local "D:\bug-cases\APP-42"
```

`prepare-local` 是人工批处理或最终兜底命令，不是 Agent 默认路径。它把归档内容
全量写到归档旁的 `<归档文件名>.unpacked/`，把索引和内部状态写到
Case 下的 `.bug-agent/`。MCP Server 模式默认采用相同布局，也可通过
`LOG_ANALYZER_WORK_ROOT` 集中保存索引。`--work-dir` 只改变索引和内部状态的
位置；两种模式都不会修改原始附件。

## 典型 Agent 调用顺序

```text
1. open_case(case_path="D:/BugCases/BUG-40305")
2. inspect_case(case_id="case_xxx")
3. 根据 Issue/Case 的症状、问题时间和附件规模选择候选归档
4. inspect_archive(case_id="case_xxx", artifact_id="artifact_xxx")
5. 根据时间窗口、日志域、文件名和大小选择 member_id
6. extract_archive_members(
     case_id="case_xxx",
     artifact_id="artifact_xxx",
     member_ids=["member_xxx", "member_yyy"]
   )
7. build_index(case_id="case_xxx", artifact_ids=["artifact_selected"])
8. parse_diagnostics(case_id="case_xxx")
9. extract_timeline(
     case_id="case_xxx",
     anchors=["bootanimation", "SurfaceFlinger", "backlight"]
   )
10. search_evidence(case_id="case_xxx", query="fence timeout")
11. 若证据不足，返回第 4 步逐步扩大时间窗口、日志域或成员范围
12. Agent 基于 Evidence 生成 RCAReport
```

Agent 不应默认调用 `prepare_case`。只有用户明确要求完整准备，或多轮选择性解压、
索引和检索后仍无法判断必要成员时，才使用它全量展开。CLI 的 `prepare-local` 与
`collect-jira --prepare` 采用同样的显式全量兜底语义。

报告推理属于 Agent Core；本 MCP 只负责提供事实和证据，不再提供
`generate_report` 工具。

`extract_timeline.anchors` 是必填参数，应由当前领域 Skill 显式提供。MCP 不
内置黑屏、启动或 ANR 等调查策略。

## V1 → V2 迁移

| V1 | V2 |
|---|---|
| 每个工具接收 `log_dirs` | 先 `open_case`，后续使用 `case_id` |
| Agent 直接操作绝对路径 | Server 管理路径，Agent 使用 Artifact ID |
| `search_log` | `search_evidence` |
| `extract_time_window` | `extract_timeline` |
| `parse_selinux_avc` / `parse_kernel_stack` | `parse_diagnostics` |
| `generate_report` | Agent 输出 RCAReport，Renderer 负责格式化 |
| 无结果返回错误 | 无结果返回成功空集合 |

V1 的纯 Python 解析函数暂时保留在 `tools.py`，用于回归参考；MCP Server
默认只暴露 V2 的九个工具。

## 项目结构

```text
src/log_analyzer/
├── server.py          # MCP 边界与工具注册
├── service.py         # V2 领域服务
├── case_registry.py   # 服务端根目录、Case 和 Artifact 注册
├── archive_manager.py # 安全归档展开、缓存与资源预算
├── log_index.py       # SQLite 分块索引与超大日志检索
├── domain.py          # Tool Input 与领域数据模型
├── security.py        # 路径和大小安全检查
├── models.py          # V1 兼容模型与 ToolResult
├── errors.py          # 统一错误结果
└── tools.py           # V1 纯 Python 解析函数（不再直接暴露）
```

协议无关的解析原语位于 `../log-analysis-core/src/log_analysis_core/`。
