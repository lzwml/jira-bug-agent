# tools/

打包的第三方工具，用于 Bug 分析过程中的特定解析任务。

## 目录说明

| 工具 | 用途 | 平台 |
|------|------|------|
| `aee_extract.exe` | MTK AEE DB 解析工具，用于从 `db.XX.NE` / `db.XX.ANR` 等 AEE 导出文件中提取/解码日志 | Windows |

## 工具用法

### aee_extract.exe

```bash
# 用法：将 db 文件路径作为参数传入
./aee_extract.exe <db_file>

# 示例
./aee_extract.exe db.03.ANR-fedeaa2ad13f0d63.dbg
```

执行后会在同目录生成 `<db_file>.DEC/` 目录，包含解码后的所有日志文件（`SYS_KERNEL_LOG`、`SWT_JBT_TRACES`、`__exp_main.txt` 等）。

**注意**：`.DEC` 输出目录已加入 `.gitignore`，不会被提交到 Git。

## 添加新工具

- 将工具文件直接放入此目录
- 在上方表格中记录工具名称、用途和平台
- 在下方补充命令行用法
- 工具文件应提交到 Git（不在 `.gitignore` 中排除）
- 工具的输出目录/临时文件应加入 `.gitignore`