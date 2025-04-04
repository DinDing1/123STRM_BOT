from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from p123 import P123Client, check_response, P123OSError
import logging
from datetime import datetime, timedelta, timezone
import errno
import sqlite3
from contextlib import closing
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import os

# 读取 VERSION 文件中的版本号
def get_version():
    """从 VERSION 文件中读取版本号"""
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "未知版本"

# 获取版本号
VERSION = get_version()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("direct_link_service.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 禁用 httpx 的日志
logging.getLogger("httpx").setLevel(logging.WARNING)

# 设置 APScheduler 的日志级别为 WARNING
scheduler_logger = logging.getLogger('apscheduler')
scheduler_logger.setLevel(logging.WARNING)

# 输出欢迎消息
logger.info(
    "\n-----------------------------------------\n"
    "   欢迎使用123网盘直连服务\n\n"
    "                      版本号：{}\n"
    "-----------------------------------------".format(VERSION)
)

# 初始化客户端（关键修改点）
client = P123Client(
    passport=os.getenv("P123_PASSPORT"),
    password=os.getenv("P123_PASSWORD")
)
# 强制模拟安卓客户端
client.headers.update({
    "platform": "android",
    "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-G988B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "origin": "https://m.123pan.com",
    "x-requested-with": "com.cloud123.pan"
})
token_expiry = None  # 用于存储 Token 的过期时间

# SQLite 缓存数据库路径
DB_DIR = "/app/data"
DB_PATH = os.path.join(DB_DIR, "cache.db")

def init_db():
    """初始化 SQLite 数据库"""
    os.makedirs(DB_DIR, exist_ok=True)
    
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            size INTEGER NOT NULL,
            etag TEXT NOT NULL,
            download_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP GENERATED ALWAYS AS (DATETIME(created_at, '+20 hours')) STORED
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_main ON cache (file_name, size, etag)''')
        conn.commit()

init_db()

def clear_expired_entries():
    """清理过期条目"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cache WHERE expires_at < datetime('now')")
        conn.commit()

def clear_all_cache():
    """全量清理"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cache")
        conn.commit()
        logger.info("已清空全部缓存")

# 定时任务
scheduler = BackgroundScheduler()
scheduler.add_job(clear_all_cache, 'interval', hours=48)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

def login_client():
    """登录并更新 Token"""
    global client, token_expiry
    try:
        login_response = client.user_login(
            {"passport": client.passport, "password": client.password, "remember": True},
            async_=False
        )
        if isinstance(login_response, dict) and login_response.get("code") == 200:
            token = login_response["data"]["token"]
            expired_at = login_response["data"].get("expire")
            token_expiry = datetime.fromisoformat(expired_at) if expired_at else datetime.now() + timedelta(days=30)
            client.token = token
            logger.info("登录成功，Token 已更新")
        else:
            logger.error(f"登录失败: {login_response}")
            raise P123OSError(errno.EIO, login_response)
    except Exception as e:
        logger.error(f"登录时发生错误: {str(e)}", exc_info=True)
        raise

def ensure_token_valid():
    """确保 Token 有效"""
    global token_expiry
    if token_expiry is None:
        logger.info("Token 未初始化，正在重新登录...")
        login_client()
    elif datetime.now() >= token_expiry.replace(tzinfo=None):
        logger.info("Token 已过期，正在重新登录...")
        login_client()

# 初始化时登录
login_client()

app = FastAPI(debug=True)

@app.get("/{uri:path}")
@app.head("/{uri:path}")
async def index(request: Request, uri: str):
    try:
        logger.info(f"收到请求: {request.url}")

        ensure_token_valid()

        if uri.count("|") < 2:
            logger.error("URI 格式错误，应为 '文件名|大小|etag'")
            return JSONResponse({"state": False, "message": "URI 格式错误，应为 '文件名|大小|etag'"}, 400)

        parts = uri.split("|")
        file_name = parts[0]
        size = int(parts[1])
        etag = parts[2].split("?")[0]
        s3_key_flag = request.query_params.get("s3keyflag", "")

        clear_expired_entries()

        # 检查缓存
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute('''SELECT download_url FROM cache 
                      WHERE file_name=? AND size=? AND etag=? 
                      AND expires_at > datetime('now')''',
                      (file_name, size, etag))
            if (row := c.fetchone()):
                logger.info(f"缓存命中: {file_name}")
                return RedirectResponse(row[0], 302)

        # 获取下载链接（关键修改点：payload无需手动添加platform）
        payload = {"FileName": file_name, "Size": size, "Etag": etag, "S3KeyFlag": s3_key_flag}
        download_resp = check_response(client.download_info(payload))
        download_url = download_resp["data"]["DownloadUrl"]

        # 写入缓存
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO cache 
                       (file_name, size, etag, download_url)
                       VALUES (?,?,?,?)''',
                       (file_name, size, etag, download_url))
            conn.commit()

        logger.info(f"302 重定向成功: {file_name}")
        return RedirectResponse(download_url, 302)

    except Exception as e:
        logger.error(f"处理失败: {str(e)}", exc_info=True)
        return JSONResponse({"state": False, "message": f"内部错误: {str(e)}"}, 500)

if __name__ == "__main__":
    from uvicorn import run
    run(app, host="0.0.0.0", port=8123, log_level="warning")
