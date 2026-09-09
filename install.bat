@echo off
rem Windows CMD 一步安装入口。
where py >nul 2>nul
if %errorlevel% equ 0 goto use_py
where python >nul 2>nul
if %errorlevel% equ 0 goto use_python
echo 安装失败：请先安装 Python 3.11 或更高版本，并勾选 Add Python to PATH。 1>&2
exit /b 1

:use_py
py -3 "%~dp0install.py" %*
exit /b %errorlevel%

:use_python
python "%~dp0install.py" %*
exit /b %errorlevel%
