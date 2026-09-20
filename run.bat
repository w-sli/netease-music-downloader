@echo off
rem 启动拾音下载器（Windows）。等价于 run.ps1，供不方便执行 PowerShell 脚本时使用。
setlocal
cd /d "%~dp0"

set "VENV=.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo 正在创建虚拟环境 %VENV% ...
  where py >nul 2>nul && (py -3 -m venv "%VENV%") || (python -m venv "%VENV%")
  if errorlevel 1 (
    echo 未找到 Python。请先安装 Python 3.10 或更高版本，并勾选 "Add Python to PATH"。
    exit /b 1
  )
)

"%VENV_PY%" -c "import flask, requests, mutagen" 2>nul
if errorlevel 1 (
  echo 正在安装依赖...
  "%VENV_PY%" -m pip install --quiet --upgrade pip
  "%VENV_PY%" -m pip install --quiet --upgrade -r requirements.txt
)

"%VENV_PY%" app.py %*
