#!/bin/bash
set -e

# 执行鉴权检查（阻塞式）
if ! ./auth_check.sh ; then
    echo -e "\033[31m[FATAL] 鉴权失败，容器终止\033[0m" >&2
    exit 1
fi

# 鉴权通过后启动主服务
exec "$@"