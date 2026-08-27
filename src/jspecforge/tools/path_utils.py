"""跨平台路径适配器。

根据 config.platform 决定路径行为：
- platform="windows": 处理 Windows↔WSL 路径转换
- platform="linux": 统一 POSIX 路径，无需转换

绝对禁止使用 sys.platform / os.name 自动检测。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List

from jspecforge.config.settings import PathsConfig


# Windows 盘符路径 → WSL /mnt/ 路径
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[/\\](.*)$")


def win_to_wsl_path(win_path: str) -> str:
    """将 Windows 路径转换为 WSL 路径格式。

    C:/Users/foo → /mnt/c/Users/foo
    C:\\Users\\foo → /mnt/c/Users/foo

    Args:
        win_path: Windows 风格路径。

    Returns:
        WSL 风格路径（/mnt/x/...）。
    """
    m = _WIN_DRIVE_RE.match(win_path.replace("\\", "/"))
    if m:
        drive_letter = m.group(1).lower()
        rest = m.group(2)
        return f"/mnt/{drive_letter}/{rest}"
    # 已经是 POSIX 路径或无法识别，直接返回
    return win_path


def wsl_to_win_path(wsl_path: str) -> str:
    """将 WSL /mnt/ 路径转换为 Windows 路径格式。

    /mnt/c/Users/foo → C:/Users/foo

    Args:
        wsl_path: WSL 风格路径（/mnt/x/...）。

    Returns:
        Windows 风格路径（C:/...）。
    """
    m = re.match(r"^/mnt/([a-z])/(.*)$", wsl_path)
    if m:
        drive_letter = m.group(1).upper()
        rest = m.group(2)
        return f"{drive_letter}:/{rest}"
    return wsl_path


def convert_path_for_tool(path: str, platform: str) -> str:
    """根据 platform 决定是否对路径进行 Windows→WSL 转换。

    用于构建 OpenJML 等需要 WSL 路径的命令参数。
    platform="windows" 时转换，platform="linux" 时原样返回。

    Args:
        path: 待转换的路径（Windows 或 POSIX 格式）。
        platform: 运行平台，"windows" 或 "linux"。

    Returns:
        工具命令中应使用的路径。
    """
    if platform == "windows":
        return win_to_wsl_path(path)
    return path


def build_classpath(*paths: str) -> str:
    """用 os.pathsep 拼接 classpath。

    Args:
        *paths: 各 classpath 组件。

    Returns:
        拼接后的 classpath 字符串。
    """
    return os.pathsep.join(paths)


def ensure_paths(paths_config: PathsConfig) -> None:
    """根据 paths 配置自动创建所有运行时目录。

    lib_dir 不自动创建（需用户手动放置 jmlruntime.jar）。
    不创建 stop_file（由停机信号创建）。
    不创建 blacklist_file（需用户预先准备）。

    Args:
        paths_config: paths 配置对象。
    """
    dirs_to_create = [
        paths_config.workspace_dir,
        paths_config.tmp_dir,
        paths_config.java_source_dir,
        paths_config.rac_output_dir,
        paths_config.pbt_bin_dir,
        paths_config.output_dir,
        paths_config.log_dir,
        paths_config.prompts_dir,
        paths_config.template_dir,
    ]

    for dir_path in dirs_to_create:
        if dir_path:
            Path(dir_path).mkdir(parents=True, exist_ok=True)
