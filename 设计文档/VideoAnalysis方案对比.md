---
tags: [video_analysis, mcp, design, ocr, vision]
created: 2026-09-03
---

# Video Analysis 方案对比

## 背景

Bug Agent 处理 Jira 工单时，录屏附件是常见证据（通常是 `.mp4`）。需要从录屏中提取信息辅助分析。

## 当前方案：Vision MCP (video-analysis-mcp)

### 架构

```
.mp4 → ffmpeg 抽帧 (JPEG) → 传给 Vision 模型 API → JSON 时间线分析
         ↑ 需要外部二进制                    ↑ 按 token 计费
```

### 工具列表

| 工具 | 作用 | 依赖 |
|---|---|---|
| `open_video` | 注册视频，返回元数据 | ffprobe |
| `inspect_video` | 读取时长/分辨率 | 无 |
| `extract_keyframes` | 按间隔采样抽帧 | ffmpeg |
| `analyze_video` | Vision 模型看图分析 | Vision API |
| `query_video` | 聚焦问题查询 | Vision API |
| `get_clip` | 截取片段（≤120s） | ffmpeg |

### 限制

- **硬依赖 ffmpeg/ffprobe**：Windows 环境需单独安装
- **采样间隔**：默认 2s，最多 24 帧，覆盖约 48 秒
- **API 费用**：每次分析传 24 张 base64 图片 + reasoning tokens
- **不支持 video 直传**：API 只接受 `image_url`，没有 `video_url` 类型

### 适用场景

| 场景 | 效果 |
|---|---|
| 页面白屏/闪退 | ✅ 状态变化明显，关键帧够抓 |
| ANR 弹窗出现 | ✅ 弹窗是静态画面 |
| 渲染撕裂 | ❌ 采样间隔太大，跳过 |
| 动画卡顿/掉帧 | ❌ 需要逐帧分析 |
| 慢性能问题 | ⚠️ 需人辅助判断 |

---

## 候选方案：OCR 路线

### 架构

```
.mp4 → OpenCV 抽帧 → OCR 提取文字 → LLM 分析文字变化
         ↑ Python 包       ↑ 开源免费        ↑ 纯文本，便宜
```

### 技术选型

| 方案 | 中文识别 | 速度 | 模型大小 | 费用 |
|---|---|---|---|---|
| **PaddleOCR** (百度) | 最强 | 中 | ~2GB | 免费 |
| **EasyOCR** | 好 | 慢 | ~1GB | 免费 |
| **Tesseract** | 一般 | 快 | ~50MB | 免费 |

### 优势

- **全本地运行**：不需要外部二进制，不需要 API key
- **零费用**：抽帧 + OCR 都是本地免费
- **针对性更强**：bug 录屏里关键信息一般是**文字**（弹窗标题、错误信息、异常堆栈、Toast 提示）
- **LLM 调用便宜**：分析文字变化是纯文本，比 vision 多模态便宜得多

### 劣势

- **纯视觉 bug 无效**：渲染异常、花屏、UI 位移等无法用文字描述
- **首次安装重**：PaddleOCR 模型 ~2GB
- **OCR 准确率依赖**：模糊/小字/特殊字体可能漏检

---

## 混合方案（推荐）

```
                 ┌─ 渲染/视觉类 bug ──→ Vision 模型分析
.mp4 → 抽帧 ──→ │
                 └─ 文字/状态类 bug ──→ OCR 提取文字 → LLM 分析
```

### 决策逻辑

1. 先抽帧（OpenCV，无需 ffmpeg）
2. 对每帧跑 OCR，提取文字变化时间线
3. 如果 OCR 发现异常文字（Error/ANR/Crash/Exception）→ 直接分析文字
4. 如果 OCR 无有效信息 → 说明是视觉类 bug，回退到 Vision 模型

### 依赖对比

| 依赖 | Vision 方案 | OCR 方案 | 混合方案 |
|---|---|---|---|
| ffmpeg/ffprobe | 必须 | 不需要 | 不需要 |
| OpenCV | 不需要 | 需要 | 需要 |
| PaddleOCR | 不需要 | 需要 | 需要 |
| Vision API | 需要 | 不需要 | 按需回退 |
| 外部二进制 | 必须 | 无 | 无 |

---

## 待决策

- [ ] 是否采用混合方案？
- [ ] OCR 引擎选 PaddleOCR 还是 EasyOCR？
- [ ] 是否需要保留 ffmpeg 路径作为 fallback？
- [ ] 关键帧采样策略：固定间隔 vs 场景变化检测？

---

## 相关文档

- [[video-analysis-mcp README]]
- [[MCP协议测试总结]]