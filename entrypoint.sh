#!/bin/sh
set -e

CONFIG_DIR="/app/config"
CONFIG_FILE="$CONFIG_DIR/config.ini"
EXAMPLE_FILE="$CONFIG_DIR/config.ini.example"

# 如果 config.ini 不存在，从样例复制
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$EXAMPLE_FILE" ]; then
        cp "$EXAMPLE_FILE" "$CONFIG_FILE"
        echo "[entrypoint] 已从 config.ini.example 生成 config.ini，请修改配置后重启容器"
        echo "[entrypoint] docker compose restart"
        exit 0
    else
        echo "[entrypoint] 错误: 找不到 config.ini.example"
        exit 1
    fi
fi

exec python3 -u qbit_to_xunlei.py
