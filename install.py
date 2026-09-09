#!/usr/bin/env python3
"""hello-sql 跨平台一步安装器（仅依赖 Python 标准库）。"""

from __future__ import annotations

import argparse
import ntpath
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import venv


APP_NAME = "hello-sql"
MIN_PYTHON = (3, 11)
MANAGED_MARKER = "hello-sql managed launcher"
PATH_BLOCK_START = "# >>> hello-sql installer >>>"
PATH_BLOCK_END = "# <<< hello-sql installer <<<"


class InstallError(RuntimeError):
    """可以向用户直接展示的安装失败。"""


def system_name() -> str:
    return platform.system()


def install_root(system: str | None = None) -> Path:
    """返回应用运行环境目录；用户数据仍由 main.py 单独管理。"""
    override = os.environ.get("HELLO_SQL_INSTALL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    current = system or system_name()
    if current == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_NAME
    if current == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def command_dir(system: str | None = None) -> Path:
    override = os.environ.get("HELLO_SQL_BIN_DIR")
    if override:
        return Path(override).expanduser().resolve()
    current = system or system_name()
    if current == "Windows":
        return install_root(current) / "bin"
    return Path.home() / ".local" / "bin"


def venv_python(runtime: Path, system: str | None = None) -> Path:
    return runtime / ("Scripts/python.exe" if (system or system_name()) == "Windows" else "bin/python")


def installed_command(runtime: Path, system: str | None = None) -> Path:
    return runtime / ("Scripts/hello-sql.exe" if (system or system_name()) == "Windows" else "bin/hello-sql")


def run(command: list[str], description: str) -> None:
    print(f"\n→ {description}")
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as error:
        raise InstallError(f"找不到命令：{command[0]}") from error
    except subprocess.CalledProcessError as error:
        raise InstallError(f"{description}失败（退出码 {error.returncode}）") from error


def create_runtime(runtime: Path) -> None:
    runtime.parent.mkdir(parents=True, exist_ok=True)
    if not venv_python(runtime).exists():
        print(f"→ 创建独立运行环境：{runtime}")
        try:
            venv.EnvBuilder(with_pip=True).create(runtime)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError(
                "无法创建 Python 虚拟环境。Linux 请确认已安装 python3-venv；"
                "Windows/macOS 请确认 Python 安装完整。"
            ) from error


def quote_sh(path: Path) -> str:
    return shlex.quote(str(path))


def launcher_text(runtime: Path, system: str | None = None) -> tuple[str, str]:
    current = system or system_name()
    executable = installed_command(runtime, current)
    if current == "Windows":
        return (
            "hello-sql.cmd",
            f"@echo off\r\nrem {MANAGED_MARKER}\r\nchcp 65001 >nul\r\n\"{executable}\" %*\r\n",
        )
    return (
        "hello-sql",
        f"#!/bin/sh\n# {MANAGED_MARKER}\nexec {quote_sh(executable)} \"$@\"\n",
    )


def create_launcher(runtime: Path, bin_dir: Path, *, force: bool = False, system: str | None = None) -> Path:
    name, content = launcher_text(runtime, system)
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / name
    if launcher.exists() and MANAGED_MARKER not in launcher.read_text(encoding="utf-8", errors="ignore"):
        if not force:
            raise InstallError(
                f"命令文件已经存在且不是本安装器创建的：{launcher}\n"
                "如确认可以覆盖，请重新运行并加 --force。"
            )
    temporary = launcher.with_name(f".{launcher.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="")
    if (system or system_name()) != "Windows":
        temporary.chmod(0o755)
    temporary.replace(launcher)
    return launcher


def path_contains(bin_dir: Path, value: str | None = None, *, system: str | None = None) -> bool:
    current = system or system_name()
    raw = value if value is not None else os.environ.get("PATH", "")
    path_module = ntpath if current == "Windows" else os.path
    separator = ";" if current == "Windows" else os.pathsep
    wanted = path_module.normcase(path_module.normpath(str(bin_dir)))
    return any(
        path_module.normcase(path_module.normpath(part.strip('"'))) == wanted
        for part in raw.split(separator) if part
    )


def append_managed_block(config: Path, line: str) -> bool:
    """幂等写入 PATH 配置；已有本安装器区块时不重复追加。"""
    try:
        old = config.read_text(encoding="utf-8") if config.exists() else ""
        if PATH_BLOCK_START in old:
            return False
        config.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not old or old.endswith("\n") else "\n"
        with config.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{prefix}\n{PATH_BLOCK_START}\n{line}\n{PATH_BLOCK_END}\n")
        return True
    except OSError as error:
        raise InstallError(f"无法更新 shell 配置 {config}：{error}") from error


def configure_posix_path(bin_dir: Path) -> tuple[bool, Path | None]:
    if path_contains(bin_dir):
        return False, None
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "fish":
        config = Path.home() / ".config" / "fish" / "config.fish"
        changed = append_managed_block(config, f"fish_add_path {quote_sh(bin_dir)}")
    elif shell == "zsh":
        config = Path.home() / ".zshrc"
        changed = append_managed_block(config, f"export PATH={quote_sh(bin_dir)}:\"$PATH\"")
    elif shell == "bash":
        config = Path.home() / (".bash_profile" if system_name() == "Darwin" else ".bashrc")
        changed = append_managed_block(config, f"export PATH={quote_sh(bin_dir)}:\"$PATH\"")
    else:
        config = Path.home() / (".zshrc" if system_name() == "Darwin" else ".profile")
        changed = append_managed_block(config, f"export PATH={quote_sh(bin_dir)}:\"$PATH\"")
    return changed, config


def configure_windows_path(bin_dir: Path) -> bool:
    """加入当前用户 PATH，并通知桌面环境；不需要管理员权限。"""
    try:
        import winreg
    except ImportError as error:  # pragma: no cover - 只可能在非 Windows 误调用
        raise InstallError("当前 Python 不支持 Windows 注册表") from error
    key_path = r"Environment"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        if path_contains(bin_dir, current, system="Windows"):
            return False
        updated = f"{current};{bin_dir}" if current else str(bin_dir)
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    try:  # 让之后启动的 GUI 终端也能更快读取新 PATH。
        import ctypes
        HWND_BROADCAST, WM_SETTINGCHANGE = 0xFFFF, 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x0002, 5000, None
        )
    except (AttributeError, OSError):
        pass
    return True


def verify(runtime: Path, system: str) -> str:
    command = installed_command(runtime, system)
    try:
        result = subprocess.run(
            [str(command), "--version"], check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InstallError(f"安装后验证失败：{error}") from error
    return result.stdout.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="安装 hello-sql 到当前用户")
    parser.add_argument("--force", action="store_true", help="覆盖同名的非 hello-sql 启动文件")
    parser.add_argument("--editable", action="store_true", help="开发模式：源码修改立即生效")
    parser.add_argument("--no-path", action="store_true", help="创建命令，但不自动修改用户 PATH")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.version_info < MIN_PYTHON:
        print("安装失败：hello-sql 需要 Python 3.11 或更高版本。", file=sys.stderr)
        return 1
    current = system_name()
    if current not in {"Darwin", "Linux", "Windows"}:
        print(f"安装失败：暂不支持当前系统 {current}。", file=sys.stderr)
        return 1
    project = Path(__file__).resolve().parent
    if not (project / "pyproject.toml").is_file():
        print("安装失败：请将安装脚本放在完整的 hello-sql 项目根目录运行。", file=sys.stderr)
        return 1
    root = install_root(current)
    runtime = root / "runtime"
    bin_dir = command_dir(current)
    try:
        print(f"hello-sql 安装器 · {current}")
        print(f"安装位置：{root}")
        create_runtime(runtime)
        python = venv_python(runtime, current)
        run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], "更新安装工具")
        install_args = [str(python), "-m", "pip", "install", "--upgrade"]
        if args.editable:
            install_args.append("--editable")
        install_args.append(str(project))
        run(install_args, "安装 hello-sql 及界面依赖")
        launcher = create_launcher(runtime, bin_dir, force=args.force, system=current)
        changed, config = False, None
        if not args.no_path:
            if current == "Windows":
                changed = configure_windows_path(bin_dir)
            else:
                changed, config = configure_posix_path(bin_dir)
        detected = verify(runtime, current)
    except (InstallError, OSError) as error:
        print(f"\n安装失败：{error}", file=sys.stderr)
        print("请检查网络连接、Python/pip 配置和目录权限后重试。", file=sys.stderr)
        return 1

    print(f"\n✓ 安装成功：{detected}")
    print(f"✓ 启动命令：{launcher}")
    if args.no_path:
        print(f"PATH 未修改；可直接运行：{launcher}")
    elif changed:
        if current == "Windows":
            print("请关闭并重新打开终端，然后输入：hello-sql")
        else:
            print(f"PATH 已写入 {config}；请重开终端，然后输入：hello-sql")
    else:
        print("现在可以输入：hello-sql")
    print(f"数据库默认保存在：{Path.home() / '.hello-sql' / 'data'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
