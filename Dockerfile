FROM python:3.11-slim

WORKDIR /app

# 安装依赖
RUN pip install --no-cache-dir requests websockets

# 复制脚本
COPY xunlei_downloader.py .
COPY qbit_to_xunlei.py .


# 配置文件通过 volume 挂载
ENV CONFIG_PATH=/app/config/config.ini

CMD ["python3", "-u", "qbit_to_xunlei.py"]
