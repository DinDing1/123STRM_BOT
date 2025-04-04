# 第一阶段：构建环境
FROM python:3.12-slim as builder

WORKDIR /app

# 安装编译依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev libmagic-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir --user -r requirements.txt

# 第二阶段：运行时环境
FROM python:3.12-slim

WORKDIR /app

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 libmagic1 libcap2-bin \
    && setcap cap_net_bind_service=+ep /usr/local/bin/uvicorn \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖
COPY --from=builder /root/.local /root/.local

# 环境变量
ENV PATH=/root/.local/bin:$PATH \
    TZ=Asia/Shanghai \
    P123_STORAGE_QUOTA=1099511627776 \
    P123_STORAGE_USED=1048576 \
    P123_VIP_EXPIRE=253402185600

# 复制应用文件
COPY direct_link_service.py .
COPY VERSION .

# 初始化文件系统
RUN mkdir -p /app/data \
    && chmod 777 /app/data \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s \
    CMD curl -f http://localhost:8123/healthcheck || exit 1

CMD ["uvicorn", "direct_link_service:app", "--host", "0.0.0.0", "--port", "8123", "--log-level", "warning"]
