FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests websockets

COPY xunlei_downloader.py .
COPY qbit_to_xunlei.py .
COPY config/config.ini.example config/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENV CONFIG_PATH=/app/config/config.ini

ENTRYPOINT ["./entrypoint.sh"]
