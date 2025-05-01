# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（如果需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制项目文件
COPY . .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 设置环境变量默认值
ENV OUTPUT_ROOT=/app/strm_output

# 创建输出目录
RUN mkdir -p ${OUTPUT_ROOT}

# 启动命令
CMD ["python", "your_script_name.py"]  # 替换为实际脚本文件名