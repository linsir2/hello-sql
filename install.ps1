# Windows PowerShell 一步安装入口。
$ErrorActionPreference = "Stop"
$Installer = Join-Path $PSScriptRoot "install.py"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $Installer @args
    exit $LASTEXITCODE
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $Installer @args
    exit $LASTEXITCODE
}

Write-Error "安装失败：请先安装 Python 3.11 或更高版本，并勾选 Add Python to PATH。"
exit 1
