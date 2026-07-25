FROM python:3.11-slim

WORKDIR /app

# 安装 Playwright Chromium 所需的系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests websockets playwright

# 安装 Playwright Chromium 及其系统依赖
RUN playwright install --with-deps chromium

COPY xunlei_downloader.py .
COPY xunlei_playwright.py .
COPY qbit_to_xunlei.py .

# 保存模板到非挂载目录
COPY config/config.ini.example /app/config.ini.example

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV CONFIG_PATH=/app/config/config.ini

ENTRYPOINT ["./entrypoint.sh"]
