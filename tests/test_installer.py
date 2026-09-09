"""跨平台安装器的目录、启动脚本及 PATH 配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

import install


def test_platform_install_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("HELLO_SQL_INSTALL_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert install.install_root("Darwin") == tmp_path / "home" / "Library" / "Application Support" / "hello-sql"
    assert install.install_root("Linux") == tmp_path / "xdg" / "hello-sql"
    assert install.install_root("Windows") == tmp_path / "local" / "hello-sql"


def test_install_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HELLO_SQL_INSTALL_DIR", str(tmp_path / "custom"))
    assert install.install_root("Linux") == tmp_path / "custom"


def test_posix_launcher_quotes_spaces_and_is_executable(tmp_path):
    runtime = tmp_path / "runtime with spaces"
    launcher = install.create_launcher(runtime, tmp_path / "bin", system="Darwin")
    content = launcher.read_text()
    assert install.MANAGED_MARKER in content
    assert "runtime with spaces/bin/hello-sql" in content
    assert launcher.stat().st_mode & 0o111


def test_windows_launcher_forwards_arguments(tmp_path):
    runtime = tmp_path / "runtime"
    launcher = install.create_launcher(runtime, tmp_path / "bin", system="Windows")
    assert launcher.name == "hello-sql.cmd"
    content = launcher.read_text()
    assert "chcp 65001" in content
    assert '"' + str(runtime / "Scripts" / "hello-sql.exe") + '" %*' in content


def test_launcher_collision_requires_force(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "hello-sql"
    launcher.write_text("someone else's command")
    with pytest.raises(install.InstallError, match="--force"):
        install.create_launcher(tmp_path / "runtime", bin_dir, system="Linux")
    install.create_launcher(tmp_path / "runtime", bin_dir, force=True, system="Linux")
    assert install.MANAGED_MARKER in launcher.read_text()


def test_managed_path_block_is_idempotent(tmp_path):
    config = tmp_path / ".zshrc"
    config.write_text("export EXISTING=1")
    assert install.append_managed_block(config, 'export PATH="/example:$PATH"')
    assert not install.append_managed_block(config, 'export PATH="/example:$PATH"')
    text = config.read_text()
    assert text.count(install.PATH_BLOCK_START) == 1
    assert text.startswith("export EXISTING=1\n")


def test_path_detection(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"/usr/bin:{bin_dir}:/bin")
    assert install.path_contains(bin_dir, system="Linux")
    assert not install.path_contains(tmp_path / "other", system="Linux")


def test_windows_path_detection_uses_semicolon_and_is_case_insensitive():
    assert install.path_contains(
        Path(r"C:\Users\Demo\hello-sql\bin"),
        r"C:\Windows;C:\USERS\DEMO\HELLO-SQL\BIN",
        system="Windows",
    )


def test_version_guard_message(monkeypatch, capsys):
    monkeypatch.setattr(install.sys, "version_info", (3, 10))
    assert install.main([]) == 1
    assert "Python 3.11" in capsys.readouterr().err
