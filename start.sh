#!/bin/bash
# eq-web 前台启动（调试用）；日常使用请用 systemctl --user start eq-web.service
#
# 必须优先用项目自带 venv: 系统 /usr/bin/python3 没有 numpy/scipy,
# 而耳机模拟功能需要它们 (缺了只有 /api/sim/run 会失败, 面板其余功能正常)。
cd "$(dirname "$0")" || exit 1
PY=./.venv/bin/python3
if [ ! -x "$PY" ]; then
  PY=/usr/bin/python3
  echo "[警告] 未找到 .venv, 回退到 $PY —— 耳机模拟将不可用" >&2
  echo "       修复: /usr/bin/python3 -m venv .venv && .venv/bin/pip install numpy scipy" >&2
fi
exec "$PY" server.py
