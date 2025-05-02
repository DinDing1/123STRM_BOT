# 第一阶段：构建环境
FROM python:3.12-slim as builder

WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装所有Python依赖
RUN pip install --no-cache-dir --user -r requirements.txt

# 第二阶段：运行时环境
FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 创建工作目录
WORKDIR /app
RUN mkdir -p \
    /app/data \
    /app/strm_output \
    /var/log/supervisor

# 从构建阶段复制已安装的依赖
COPY --from=builder /root/.local /root/.local

# 复制应用文件
COPY 123strm.py direct_link_service.py VERSION ./
COPY supervisord.conf /etc/supervisor/supervisord.conf

# 设置环境变量
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OUTPUT_ROOT=/app/strm_output \
    DB_DIR=/app/data

# 设置文件权限
RUN chmod 777 /app/data /app/strm_output

# 使用supervisord作为入口点
ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]