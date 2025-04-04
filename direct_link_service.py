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
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    return "未知版本"

VERSION = get_version()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("direct_link_service.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
scheduler_logger = logging.getLogger('apscheduler')
scheduler_logger.setLevel(logging.WARNING)

logger.info(
    "\n-----------------------------------------\n"
    "   欢迎使用123网盘直连服务\n\n"
    "                      版本号：{}\n"
    "-----------------------------------------".format(VERSION)
)

# 初始化客户端
client = P123Client(
    passport=os.getenv("P123_PASSPORT"),
    password=os.getenv("P123_PASSWORD")
)
client.headers.update({
    "platform": "android",
    "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-G988B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "origin": "https://m.123pan.com",
    "x-requested-with": "com.cloud123.pan"
})
token_expiry = None

# 数据库配置
DB_DIR = "/app/data"
DB_PATH = os.path.join(DB_DIR, "cache.db")

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            size INTEGER NOT NULL,
            etag TEXT NOT NULL,
            s3keyflag TEXT NOT NULL,
            download_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP GENERATED ALWAYS AS (DATETIME(created_at, '+20 hours')) STORED
        )''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_main ON cache (file_name, size, etag, s3keyflag)''')
        conn.commit()

init_db()

def clear_expired_entries():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cache WHERE expires_at < datetime('now')")
        conn.commit()

def clear_old_cache():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM cache WHERE id IN (SELECT id FROM cache ORDER BY created_at ASC LIMIT 100)")
        conn.commit()
        logger.info("已清理最旧100条缓存")

scheduler = BackgroundScheduler()
scheduler.add_job(clear_expired_entries, 'interval', minutes=30)
scheduler.add_job(clear_old_cache, 'interval', hours=6)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

def login_client():
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
            raise P123OSError(errno.EIO, login_response)
    except Exception as e:
        logger.error(f"登录失败: {str(e)}", exc_info=True)
        raise

def ensure_token_valid():
    global token_expiry
    if token_expiry is None or datetime.now() >= token_expiry.replace(tzinfo=None):
        logger.info("Token无效或过期，正在重新登录...")
        login_client()

login_client()

app = FastAPI(debug=True)

@app.get("/{uri:path}")
@app.head("/{uri:path}")
async def index(request: Request, uri: str):
    try:
        logger.info(f"收到请求: {request.url}")
        ensure_token_valid()

        # 严格校验URI格式
        if uri.count("|") < 2 or "?" not in uri:
            logger.error(f"非法URI格式: {uri}")
            return JSONResponse(
                {"state": False, "message": "URI格式应为 文件名|大小|etag?s3keyflag"},
                status_code=400
            )

        # 解析参数
        base_part, s3_key_flag = uri.split("?", 1)
        parts = base_part.split("|")
        if len(parts) != 3:
            logger.error(f"参数数量错误: {uri}")
            return JSONResponse(
                {"state": False, "message": "需要3个参数: 文件名|大小|etag"},
                status_code=400
            )

        file_name, size_str, etag = parts
        try:
            size = int(size_str)
        except ValueError:
            logger.error(f"无效文件大小: {size_str}")
            return JSONResponse(
                {"state": False, "message": "文件大小必须为整数"},
                status_code=400
            )

        # 构造payload
        payload = {
            "Etag": etag,
            "S3KeyFlag": s3_key_flag,
            "FileName": file_name,
            "Size": size,
            "FileID": 0,
            "Type": 0,
            "driveId": 0
        }

        # 检查缓存
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute('''SELECT download_url FROM cache 
                      WHERE file_name=? AND size=? AND etag=? AND s3keyflag=?
                      AND expires_at > datetime('now')''',
                      (file_name, size, etag, s3_key_flag))
            if (row := c.fetchone()):
                logger.info(f"缓存命中: {file_name}")
                return RedirectResponse(row[0], 302)

        # 获取下载链接
        download_resp = check_response(client.download_info(payload))
        download_url = download_resp["data"]["DownloadUrl"]

        # 写入缓存
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO cache 
                       (file_name, size, etag, s3keyflag, download_url)
                       VALUES (?,?,?,?,?)''',
                       (file_name, size, etag, s3_key_flag, download_url))
            conn.commit()

        logger.info(f"302重定向成功: {file_name}")
        return RedirectResponse(download_url, 302)

    except P123OSError as e:
        logger.error(f"云盘接口错误: {str(e)}")
        return JSONResponse(
            {"state": False, "message": f"云盘服务错误: {e.args[1].get('message', '未知错误')}"},
            status_code=502
        )
    except Exception as e:
        logger.error(f"处理失败: {str(e)}", exc_info=True)
        return JSONResponse(
            {"state": False, "message": f"内部服务器错误: {str(e)}"},
            status_code=500
        )

if __name__ == "__main__":
    from uvicorn import run
    run(app, host="0.0.0.0", port=8123, log_level="warning")
