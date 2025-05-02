# 第一阶段：通用依赖构建
FROM python:3.12-slim as builder

WORKDIR /app

# 合并所有依赖
COPY requirements.txt .

# 安装所有Python依赖到用户目录
RUN pip install --no-cache-dir --user -r requirements.txt

# 第二阶段：运行时环境
FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# 配置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

# 配置目录结构
WORKDIR /app
RUN mkdir -p \
    /app/data \
    /app/strm_output \
    /var/log/supervisor

# 复制应用文件
COPY --from=builder /root/.local /root/.local
COPY 123strm.py direct_link_service.py VERSION ./
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 设置环境变量
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OUTPUT_ROOT=/app/strm_output \
    DB_DIR=/app/data

# 文件权限设置
RUN chmod 777 /app/data /app/strm_output

# 使用supervisord管理多进程
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]