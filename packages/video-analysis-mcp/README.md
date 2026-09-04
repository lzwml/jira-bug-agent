# video-analysis-mcp

面向 Bug Agent 的受控视频证据 MCP。它只允许打开配置根目录内的录屏文件，以稳定
`video_id` 管理后续操作；不向模型返回任何绝对路径。

## 工具

| 工具 | 作用 |
| --- | --- |
| `open_video` | 注册 Case 内的视频并返回安全元数据 |
| `inspect_video` | 读取时长、尺寸和音轨信息 |
| `extract_keyframes` | 有上限地导出带毫秒时间点的关键帧 |
| `analyze_video` | 让已配置的视觉模型输出 JSON 摘要与事件时间线 |
| `query_video` | 对录屏进行聚焦问题查询 |
| `get_clip` | 导出最长 120 秒的待复核问题片段 |

`analyze_video` 和 `query_video` 需要一个兼容 OpenAI Chat Completions 的视觉端点，
并要求模型接受 `image_url` 的 data URL。未配置该端点时，元数据、抽帧和裁剪仍可用，
但视觉分析会返回明确配置错误。

## 安全与配置

```powershell
$env:VIDEO_ANALYZER_ALLOWED_ROOTS = 'D:\bug-cases\APP-42'
$env:VIDEO_ANALYZER_WORK_ROOT = 'D:\bug-agent-cache\video-analysis'
$env:VIDEO_ANALYZER_PROVIDER_URL = 'https://your-vision-gateway.example/v1'
$env:VIDEO_ANALYZER_PROVIDER_API_KEY = 'your-key'
$env:VIDEO_ANALYZER_PROVIDER_MODEL = 'your-vision-model'
python -m video_analysis.server
```

服务端强制限制视频文件大小（默认 2 GiB）、视频时长（默认 30 分钟）、单次帧数
（最多 120）和裁剪时长（最多 120 秒）。关键帧与片段始终写入
`VIDEO_ANALYZER_WORK_ROOT`，原始视频不会被改写。
