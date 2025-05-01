FROM python:3.12-slim as builder

WORKDIR /app
COPY requirements.txt .

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libffi-dev \
    libssl-dev \
    zlib1g-dev && \
    pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV OUTPUT_ROOT=/app/strm_output

RUN mkdir -p ${OUTPUT_ROOT} && \
    chmod 777 ${OUTPUT_ROOT}

# 启动命令
CMD ["python", "123strm.py"] 