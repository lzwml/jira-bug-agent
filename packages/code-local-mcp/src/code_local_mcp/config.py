"""code-local-mcp 环境配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeLocalConfig:
    """本地源码根路径映射。从环境变量 LOCODE_ROOT 或 LOCODE_MAP 加载。"""

    roots: dict[str, Path]  # project_name → local_path

    @classmethod
    def from_environment(cls) -> "CodeLocalConfig":
        try:
            from jira_bug_mcp.config import load_local_env
            load_local_env()
        except ImportError:
            pass

        roots: dict[str, Path] = {}
        # 方式1: LOCODE_MAP = "android_b=g:/work/N60/b_android,yocto=g:/work/N60/yocto"
        map_str = os.getenv("LOCODE_MAP", "").strip()
        if map_str:
            for pair in map_str.split(","):
                pair = pair.strip()
                if "=" in pair:
                    name, root = pair.split("=", 1)
                    p = Path(root.strip()).expanduser().absolute()
                    if p.is_dir():
                        roots[name.strip()] = p

        # 方式2: LOCODE_ROOT = "g:/work/N60" → 自动发现子目录
        base = os.getenv("LOCODE_ROOT", "").strip()
        if base and not roots:
            base_path = Path(base).expanduser().absolute()
            if base_path.is_dir():
                for child in base_path.iterdir():
                    if child.is_dir() and not child.name.startswith("."):
                        roots[child.name] = child

        if not roots:
            raise ValueError(
                "未配置本地源码路径。设置 LOCODE_MAP 或 LOCODE_ROOT。"
            )

        return cls(roots=roots)

    def resolve(self, project: str, path: str) -> Path:
        """将 OpenGrok 项目名 + 文件路径转换为本地路径。"""
        local_root = self.roots.get(project)
        if local_root is None:
            raise ValueError(
                f"未找到项目 '{project}' 的本地路径映射。"
                f"可用项目: {', '.join(sorted(self.roots))}"
            )
        # 安全检查：路径不能包含遍历序列
        clean = path.lstrip("/").replace("\\", "/")
        if ".." in clean.split("/"):
            raise ValueError(f"路径包含非法遍历: {path}")
        return (local_root / clean).absolute()