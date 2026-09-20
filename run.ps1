<#
  启动拾音下载器（Windows PowerShell）：按需创建虚拟环境、安装依赖，然后运行。
  用法： powershell -ExecutionPolicy Bypass -File run.ps1
  可传参数，例如： powershell -ExecutionPolicy Bypass -File run.ps1 --port 36523
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$Venv = ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"

function Get-PythonCommand {
    foreach ($candidate in @("py", "python", "python3")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            if ($candidate -eq "py") { return @("py", "-3") }
            return @($candidate)
        }
    }
    throw "未找到 Python。请先安装 Python 3.10 或更高版本，并勾选“Add Python to PATH”。"
}

if (-not (Test-Path $VenvPython)) {
    $python = Get-PythonCommand
    Write-Host "正在创建虚拟环境 $Venv …"
    & $python[0] $python[1..($python.Length - 1)] -m venv $Venv
}

& $VenvPython -c "import flask, requests, mutagen" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "正在安装依赖…"
    & $VenvPython -m pip install --quiet --upgrade pip
    & $VenvPython -m pip install --quiet --upgrade -r requirements.txt
}

& $VenvPython app.py @args
