"""Bug Case 与 Artifact 注册表。

安全根目录由 MCP Server 配置。工具调用者只能在这些根目录内打开 Case；
注册后，后续工具通过 case_id/artifact_id 工作，不再接收裸文件路径。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .domain import Artifact, ArtifactKind, CaseInfo
from .errors import make_error, make_success
from .models import ToolResult
from .security import PathGuard


TEXT_SUFFIXES = {
    "", ".log", ".txt", ".curf", ".trace", ".out",
    ".csv", ".json", ".xml", ".prop", ".md", ".cfg", ".conf",
    ".ini", ".yaml", ".yml", ".py", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".java", ".kt", ".sh",
}
ARCHIVE_SUFFIXES = {".zip", ".gz", ".tgz", ".tar", ".7z", ".rar"}
SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", "node_modules"}


def _stable_id(prefix: str, value: str, length: int = 16) -> str:
    """根据稳定输入生成短 ID，同一路径重复注册会得到相同 ID。"""

    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:length]}"


def infer_artifact_kind(path: Path) -> ArtifactKind:
    """根据文件名语义和扩展名推断附件类型。

    这里优先看文件名，因为 Android debuglogger 经常使用 .curf 或无扩展名，
    仅靠 suffix 无法区分 logcat、kernel 和 SOS 日志。
    """

    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    if "tombstone" in name or "native_crash" in name:
        return "tombstone"
    if "anr" in name or name.startswith("traces"):
        return "anr"
    if any(token in name for token in ("kernel", "dmesg", "kmsg", "pstore", "ramoops")):
        return "kernel"
    if any(token in name for token in ("logcat", "main_log", "system_log", "events_log", "radio_log")):
        return "logcat"
    if any(token in name for token in ("perfetto", "systrace", "boottrace")) or suffix == ".trace":
        return "trace"
    if any(token in name for token in ("sos", "hypervisor", "qnx")):
        return "sos"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "binary"


class CaseRegistry:
    """进程内 Case Registry；路径授权来自服务端配置而非 Tool Call。

    Registry 同时承担 Adapter 与安全引用表的职责：
    - 外部输入：case_path；
    - 内部保存：case_id/artifact_id 到真实 Path 的映射；
    - 对模型输出：相对路径和稳定 ID。

    当前版本是进程内状态，Server 重启后需要重新调用 open_case。后续如果
    需要断点恢复，可以把这层替换为 SQLite，而不修改 Agent 的工具契约。
    """

    def __init__(self, allowed_roots: list[str], *, max_artifacts: int = 20_000):
        if not allowed_roots:
            raise ValueError("allowed_roots 不能为空")
        # allowed_roots 来自 Server 配置。绝不能从 MCP Tool 参数中读取，
        # 否则就变成“调用者自己声明自己可以访问哪里”。
        self.guard = PathGuard(allowed_roots)
        self.allowed_roots = list(self.guard.allowed_dirs)
        self.max_artifacts = max_artifacts
        self._cases: dict[str, tuple[Path, CaseInfo]] = {}
        self._artifact_paths: dict[tuple[str, str], Path] = {}

    @classmethod
    def from_environment(cls) -> "CaseRegistry":
        """从 Server 环境变量构建 Registry。

        os.pathsep 在 Windows 是分号，在 Linux/macOS 是冒号，因此同一段
        配置解析逻辑可以跨平台工作。
        """

        raw = os.environ.get("LOG_ANALYZER_ALLOWED_ROOTS", "").strip()
        roots = [path for path in raw.split(os.pathsep) if path]
        return cls(roots or [os.getcwd()])

    def open_case(self, case_path: str) -> ToolResult:
        """校验并注册 Case，建立 Artifact 清单和内部路径映射。"""

        # 第一层检查：Case 本身必须位于 Server 配置的 allowed roots 内。
        checked = self.guard.validate(case_path)
        if not checked.success:
            return checked
        resolved = Path(checked.data["resolved_path"])
        if not resolved.is_dir():
            return make_error("CASE_NOT_DIRECTORY", "Case 路径必须是已存在的目录")

        case_id = _stable_id("case", str(resolved).casefold())
        artifacts: list[Artifact] = []
        total_size = 0
        # 注册阶段只收集元数据，不读取文件内容。真正读取由具体分析工具按需完成。
        for path in resolved.rglob("*"):
            if len(artifacts) >= self.max_artifacts:
                return make_error("CASE_TOO_LARGE", f"Case 文件数量超过上限 {self.max_artifacts}")
            # 跳过开发环境和缓存目录，避免把无关文件注册成日志附件。
            relative_parts = path.relative_to(resolved).parts
            if any(part in SKIP_DIR_NAMES for part in relative_parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            # 第二层检查：解析真实路径后仍必须位于 Case 内。
            # 即使未来平台出现 junction 等重定向，也不能借此逃出 Case。
            try:
                real_path = path.resolve(strict=True)
                real_path.relative_to(resolved)
                stat = real_path.stat()
            except (OSError, ValueError):
                continue

            relative_path = real_path.relative_to(resolved).as_posix()
            kind = infer_artifact_kind(real_path)
            # ID 只依赖 Case 与相对路径；模型无需持有真实绝对路径。
            artifact_id = _stable_id("artifact", f"{case_id}:{relative_path.casefold()}")
            artifact = Artifact(
                artifact_id=artifact_id,
                name=real_path.name,
                relative_path=relative_path,
                kind=kind,
                size_bytes=stat.st_size,
                modified_at=str(int(stat.st_mtime)),
                readable_text=kind not in ("archive", "binary", "trace"),
            )
            artifacts.append(artifact)
            self._artifact_paths[(case_id, artifact_id)] = real_path
            total_size += stat.st_size

        artifacts.sort(key=lambda item: item.relative_path.casefold())
        info = CaseInfo(
            case_id=case_id,
            name=resolved.name,
            artifact_count=len(artifacts),
            total_size_bytes=total_size,
            artifacts=artifacts,
        )
        self._cases[case_id] = (resolved, info)
        return make_success({"case": info.model_dump()})

    def get_case(self, case_id: str) -> tuple[Path, CaseInfo] | None:
        return self._cases.get(case_id)

    def get_artifact_path(self, case_id: str, artifact_id: str) -> Path | None:
        return self._artifact_paths.get((case_id, artifact_id))

    def select_artifacts(
        self,
        case_id: str,
        *,
        artifact_ids: list[str] | None = None,
        artifact_kinds: list[str] | None = None,
        text_only: bool = True,
    ) -> list[Artifact]:
        """按 ID/类型选择附件；默认排除不可安全当作文本读取的文件。"""

        entry = self.get_case(case_id)
        if entry is None:
            return []
        _, info = entry
        ids = set(artifact_ids or [])
        kinds = set(artifact_kinds or [])
        return [
            artifact for artifact in info.artifacts
            if (not ids or artifact.artifact_id in ids)
            and (not kinds or artifact.kind in kinds)
            and (not text_only or artifact.readable_text)
        ]
