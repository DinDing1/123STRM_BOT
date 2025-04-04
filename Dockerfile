# 使用官方 Python 基础镜像
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 暴露端口
EXPOSE 8123

# 设置环境变量（默认值）
ENV DEBUG=false
ENV P123_PASSPORT=your_passport
ENV P123_PASSWORD=your_password

# 运行应用
CMD ["uvicorn", "direct_link_service:app", "--host", "0.0.0.0", "--port", "8123"]
