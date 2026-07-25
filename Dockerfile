FROM python:3.11-slim

WORKDIR /app

# 安装依赖
RUN pip install --no-cache-dir requests

# 复制脚本
COPY xunlei_downloader.py .
COPY qbit_to_xunlei.py .
COPY config.ini .

# 数据目录
VOLUME /app/data

# 环境变量
ENV CONFIG_PATH=/app/config.ini

# 运行
CMD ["python3", "qbit_to_xunlei.py"]
