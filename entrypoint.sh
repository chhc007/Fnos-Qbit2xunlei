#!/bin/sh
set -e

CONFIG_DIR="/app/config"

mkdir -p "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.ini" ]; then
    echo "[entrypoint] 未检测到配置文件，释放示例配置"

    cp /app/config.ini.example "$CONFIG_DIR/config.ini"

    echo "[entrypoint] 已创建 $CONFIG_DIR/config.ini"
fi

exec python3 /app/qbit_to_xunlei.py