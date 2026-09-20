#!/usr/bin/env bash
# 启动拾音下载器：按需创建虚拟环境、安装依赖，然后运行。
# Windows 请使用 run.ps1。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "正在创建虚拟环境 $VENV …"
  "$PYTHON" -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c 'import flask, requests, mutagen' 2>/dev/null; then
  echo "正在安装依赖…"
  if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    # 部分发行版的 venv 不带 pip
    "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || {
      echo "venv 缺少 pip，正在下载 get-pip.py …"
      # 用 mktemp 生成不可预测的路径，避免固定 /tmp 路径被他人预置替换
      TMP_PIP="$(mktemp -t get-pip.XXXXXX.py)"
      trap 'rm -f "$TMP_PIP"' EXIT
      curl -fsSL -o "$TMP_PIP" https://bootstrap.pypa.io/get-pip.py
      "$VENV/bin/python" "$TMP_PIP"
      rm -f "$TMP_PIP"
      trap - EXIT
    }
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade -r requirements.txt
fi

exec "$VENV/bin/python" app.py "$@"
