FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests websockets

COPY xunlei_downloader.py .
COPY qbit_to_xunlei.py .

# 保存模板到非挂载目录
COPY config/config.ini.example /app/config.ini.example

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV CONFIG_PATH=/app/config/config.ini

ENTRYPOINT ["./entrypoint.sh"]