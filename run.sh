#!/usr/bin/env bash
# 启动拾音下载器：按需创建虚拟环境、安装依赖，然后运行。
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "正在创建虚拟环境 $VENV …"
  "$PYTHON" -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c 'import flask, requests, mutagen' 2>/dev/null; then
  echo "正在安装依赖（flask、requests、mutagen）…"
  if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    # 部分发行版的 venv 不带 pip，用官方脚本补上
    "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || {
      echo "venv 缺少 pip，正在下载 get-pip.py …"
      curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
      "$VENV/bin/python" /tmp/get-pip.py
    }
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade flask requests mutagen
fi

exec "$VENV/bin/python" app.py "$@"
