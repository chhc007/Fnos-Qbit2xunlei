FROM python:3.11-slim

WORKDIR /app

# 安装 Playwright Chromium 所需的系统依赖 + tini
# tini 作为 PID 1 负责回收孤儿进程（Playwright 启动的 chromium 等），
# 否则孤儿进程会累积成僵尸并最终顶满容器 PID 上限。
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg tini \
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

# 用 tini 作为 init 进程（PID 1），自动回收子进程、转发信号
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./entrypoint.sh"]
