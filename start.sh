#!/usr/bin/env bash
# Raindrop Chart — macOS / Linux 启动脚本
# 用法：双击（macOS 需先 chmod +x start.sh），或终端里 ./start.sh
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "没找到 Python。请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/"
  exit 1
fi

echo "Raindrop Chart 正在启动……"
exec "$PY" live_server.py
