#!/usr/bin/env python3
"""
跨平台小工具：Windows / macOS 共用（拆页 Web、图片翻译 Web）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def is_windows() -> bool:
    return sys.platform == "win32"


def system_temp_dir() -> Path:
    """系统临时目录（Windows 上 TMPDIR 常未设置）。"""
    return Path(tempfile.gettempdir())


def is_temp_path(path: Path) -> bool:
    """Gradio / 系统临时路径，不能当作用户原文件目录。"""
    try:
        s = str(path.expanduser().resolve()).lower()
    except OSError:
        return True
    markers = (
        "/gradio/",
        "\\gradio\\",
        "/.gradio/",
        "\\.gradio\\",
        ".gradio_preview",
        "/tmp/",
        "\\tmp\\",
        "/var/folders/",
        "/private/var/",
        "/temp/",
        "\\temp\\",
        "/.cursor/",
        "\\.cursor\\",
        "/cache/",
        "\\cache\\",
        "/caches/",
        "\\caches\\",
        "/application support/",
        "\\application support\\",
        "/temporaryitems/",
        "\\appdata\\local\\temp",
        "\\windows\\temp",
    )
    return any(m in s for m in markers)


def search_roots() -> list[Path]:
    """在本机常用目录里查找用户文件（上传定位原 PDF 等）。"""
    home = Path.home()
    candidates = [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        home / "Dropbox",
        Path.cwd(),
    ]
    if is_windows():
        candidates.extend(
            [
                home / "OneDrive",
                home / "OneDrive" / "Documents",
                home / "OneDrive" / "Downloads",
                home / "OneDrive" / "Desktop",
            ]
        )
    else:
        candidates.extend(
            [
                home / "iCloud Drive",
                home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
            ]
        )
    return [r for r in candidates if r.is_dir()]


def collect_files_by_name_size(
    name: str,
    size: int,
    *,
    suffix: str = ".pdf",
    exclude: Path | None = None,
) -> list[Path]:
    """按文件名与大小收集候选文件（排除临时目录）。"""
    seen: set[Path] = set()
    matches: list[Path] = []

    def ok(p: Path) -> bool:
        try:
            if (
                not p.is_file()
                or p.name != name
                or p.suffix.lower() != suffix.lower()
                or p.stat().st_size != size
                or is_temp_path(p)
            ):
                return False
            if exclude and p.resolve() == exclude.resolve():
                return False
            return True
        except OSError:
            return False

    def add(p: Path) -> None:
        key = p.resolve()
        if key not in seen and ok(key):
            seen.add(key)
            matches.append(key)

    # macOS：Spotlight 全局搜索
    if not is_windows():
        safe_name = name.replace("'", "\\'")
        try:
            out = subprocess.check_output(
                [
                    "mdfind",
                    f"kMDItemFSName == '{safe_name}' && kMDItemFSSize == {size}",
                ],
                text=True,
                timeout=20,
            )
            for line in out.strip().splitlines():
                add(Path(line.strip()))
        except Exception:
            pass

    for root in search_roots():
        try:
            for p in root.rglob(name):
                add(p)
        except OSError:
            continue
    return matches


def pick_best_by_mtime(matches: list[Path], preferred_roots: list[Path]) -> Path:
    """多个同名文件时，优先常用目录里最近修改的一个。"""
    preferred = [str(r) for r in preferred_roots]
    in_preferred = [p for p in matches if any(pre in str(p) for pre in preferred)]
    pool = in_preferred or matches
    return max(pool, key=lambda p: p.stat().st_mtime)


def kill_port_processes(port: int) -> None:
    """结束占用端口的进程（主要用于 PM2 重启）。"""
    if is_windows():
        _kill_port_windows(port)
    else:
        _kill_port_unix(port)


def _kill_port_unix(port: int) -> None:
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    if not out:
        return
    for pid in out.split("\n"):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    import time

    time.sleep(0.5)
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for pid in out.split("\n"):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass


def _kill_port_windows(port: int) -> None:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"],
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    pids: set[int] = set()
    token = f":{port}"
    for line in out.splitlines():
        if token not in line.upper():
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    for pid in pids:
        if pid <= 0:
            continue
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def gradio_allowed_path_candidates() -> list[Path]:
    """Gradio 文件预览白名单候选目录。"""
    home = Path.home()
    return [
        Path.cwd(),
        system_temp_dir(),
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]


def port_busy_hint(port: int) -> str:
    if is_windows():
        return f"netstat -ano | findstr :{port}"
    return f"lsof -i :{port}"


def venv_python() -> Path:
    """当前项目虚拟环境里的 Python 路径。"""
    base = Path(__file__).resolve().parent / ".venv"
    if is_windows():
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"
