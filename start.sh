#!/bin/bash
# eq-web 前台启动（调试用）；日常使用请用 systemctl --user start eq-web.service
exec /usr/bin/python3 /home/mak1ma/eq-web/server.py
