# 第一阶段：构建Python依赖
FROM python:3.12-slim as builder

WORKDIR /app
COPY requirements.txt .

RUN pip install --no-cache-dir --user -r requirements.txt

# 第二阶段：运行时环境
FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    supervisor \
    coreutils \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 创建工作目录
WORKDIR /app
RUN mkdir -p /app/{data,config,strm_output} && \
    chmod 777 /app/{data,config,strm_output}

# 复制构建结果
COPY --from=builder /root/.local /root/.local

# 复制必要文件
COPY auth_check.sh entrypoint.sh strm_core.py direct_link_service.py VERSION ./
COPY supervisord.conf /etc/supervisor/supervisord.conf

# 设置权限
RUN chmod +x auth_check.sh entrypoint.sh

# 环境变量配置
ENV PATH="/root/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AUTH_API_URL="http://158.178.236.135:35000/verify" \ 
    DEVICE_ID_FILE="/app/config/device_id" \
    OUTPUT_ROOT="/app/strm_output" \
    DB_DIR="/app/data"

# 挂载点定义
VOLUME /app/config
VOLUME /app/data  

# 容器入口点
ENTRYPOINT ["./entrypoint.sh"]

# 默认启动命令（鉴权通过后执行）
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
