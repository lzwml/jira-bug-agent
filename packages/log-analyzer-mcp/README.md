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

其他安全约束：

- 所有工具只读；
- Case 注册时跳过符号链接；
- 解析后的真实路径必须仍在允许根目录和 Case 目录内；
- 单文件和单次调用有扫描预算；
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

## 典型 Agent 调用顺序

```text
1. open_case(case_path="D:/BugCases/BUG-40305")
2. inspect_case(case_id="case_xxx")
3. parse_diagnostics(case_id="case_xxx")
4. extract_timeline(
     case_id="case_xxx",
     anchors=["bootanimation", "SurfaceFlinger", "backlight"]
   )
5. search_evidence(case_id="case_xxx", query="fence timeout")
6. Agent 基于 Evidence 生成 RCAReport
```

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
默认只暴露 V2 的五个工具。

## 项目结构

```text
src/log_analyzer/
├── server.py          # MCP 边界与工具注册
├── service.py         # V2 领域服务
├── case_registry.py   # 服务端根目录、Case 和 Artifact 注册
├── domain.py          # Tool Input 与领域数据模型
├── security.py        # 路径和大小安全检查
├── models.py          # V1 兼容模型与 ToolResult
├── errors.py          # 统一错误结果
└── tools.py           # V1 纯 Python 解析函数（不再直接暴露）
```

协议无关的解析原语位于 `../log-analysis-core/src/log_analysis_core/`。
