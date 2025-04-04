from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from p123 import P123Client, check_response, P123OSError
import logging
from datetime import datetime, timedelta
import errno
import sqlite3
from contextlib import closing
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import os
from urllib.parse import unquote
import hashlib
import time

# ==================== 初始化配置 ====================
def get_version():
    version_file = os.path.join(os.path.dirname(__file__), "VERSION")
    return open(version_file).read().strip() if os.path.exists(version_file) else "8.8.8"  # 默认使用云盘协议版本

VERSION = get_version()

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("direct_link_service.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("123PanDirectLink")

# ==================== 客户端配置 ====================
class PanClient(P123Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device_id = hashlib.md5(os.getenv("P123_PASSPORT", "").encode()).hexdigest()
        self.update_headers()
    
    def update_headers(self):
        self.headers = {
            "accept": "application/json, text/plain, */*",
            "accept-encoding": "gzip, deflate",
            "accept-language": "zh-CN,zh;q=0.9",
            "connection": "keep-alive",
            "platform": "android",
            "user-agent": "Mozilla/5.0 (Linux; Android 13; SM-G9910) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
            "x-client-version": "3.5.0",
            "x-device-id": self.device_id,
            "x-network-type": "WIFI",
            "x-pan-version": VERSION,
            "x-request-id": self.generate_request_id()
        }
    
    def generate_request_id(self):
        return f"{int(time.time()*1000)}{os.urandom(4).hex()}"

client = PanClient(
    passport=os.getenv("P123_PASSPORT"),
    password=os.getenv("P123_PASSWORD")
)

# ==================== 数据库配置 ====================
DB_PATH = "/app/data/cache.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS file_cache (
                file_sign TEXT PRIMARY KEY,
                download_url TEXT NOT NULL,
                create_time INTEGER DEFAULT (strftime('%s','now')),
                expire_time INTEGER DEFAULT (strftime('%s','now') + 20*3600)
            )''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_uri TEXT NOT NULL,
                remote_addr TEXT,
                user_agent TEXT,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )''')

init_db()

# ==================== 服务核心逻辑 ====================
app = FastAPI(title="123云盘直链服务", debug=False)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    response = await call_next(request)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO request_log (request_uri, remote_addr, user_agent) VALUES (?,?,?)",
            (str(request.url), request.client.host, request.headers.get("user-agent"))
        )
    return response

def parse_pan_uri(uri: str) -> dict:
    """解析123云盘官方URI格式"""
    try:
        # 格式: filename|size|etag[?params]
        base, _, params = uri.partition('?')
        filename, size, etag = base.split('|', 2)
        
        # 提取S3KeyFlag (格式: s3keyflag=xxx 或直接xxx)
        s3keyflag = params.split('=', 1)[-1].split('&', 1)[0]
        
        return {
            "filename": unquote(filename),
            "size": int(size),
            "etag": etag.lower(),  # 统一小写
            "s3keyflag": s3keyflag
        }
    except Exception as e:
        logger.error(f"URI解析失败: {uri} - {str(e)}")
        raise ValueError("Invalid URI format")

@app.get("/{uri:path}", status_code=status.HTTP_302_FOUND)
@app.head("/{uri:path}")
async def handle_direct_link(request: Request, uri: str):
    try:
        # 1. 解析请求参数
        file_info = parse_pan_uri(uri)
        file_sign = f"{file_info['etag']}:{file_info['s3keyflag']}"
        
        # 2. 检查缓存
        with closing(sqlite3.connect(DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT download_url FROM file_cache WHERE file_sign=? AND expire_time > strftime('%s','now')",
                (file_sign,)
            )
            if (row := cursor.fetchone()):
                logger.info(f"[缓存命中] {file_sign[:16]}...")
                return RedirectResponse(row[0])
        
        # 3. 构造官方请求参数
        payload = {
            "Etag": file_info['etag'],
            "S3KeyFlag": file_info['s3keyflag'],
            "FileName": file_info['filename'],
            "Size": file_info['size'],
            "FileID": 0,
            "Type": 0,
            "driveId": 0,
            "_t": int(time.time() * 1000),  # 时间戳
            "_r": os.urandom(8).hex()      # 随机数
        }
        
        # 4. 获取下载链接
        resp = check_response(client.download_info(payload))
        download_url = resp["data"]["DownloadUrl"]
        
        # 5. 缓存结果
        with closing(sqlite3.connect(DB_PATH)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO file_cache (file_sign, download_url) VALUES (?,?)",
                (file_sign, download_url)
            )
        
        logger.info(f"[直链生成] {file_info['filename']}")
        return RedirectResponse(download_url)
        
    except ValueError as e:
        return JSONResponse(
            {"code": 400, "message": f"参数错误: {str(e)}"},
            status_code=status.HTTP_400_BAD_REQUEST
        )
    except P123OSError as e:
        logger.error(f"云盘接口错误: {e.args[1]}")
        return JSONResponse(
            {"code": 502, "message": "云盘服务暂时不可用"},
            status_code=status.HTTP_502_BAD_GATEWAY
        )
    except Exception as e:
        logger.error(f"系统错误: {str(e)}", exc_info=True)
        return JSONResponse(
            {"code": 500, "message": "系统内部错误"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

# ==================== 定时任务 ====================
def cleanup_tasks():
    """每日清理任务"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        # 清理过期缓存
        conn.execute("DELETE FROM file_cache WHERE expire_time <= strftime('%s','now')")
        # 保留最近7天日志
        conn.execute("DELETE FROM request_log WHERE created_at <= strftime('%s','now','-7 days')")
        logger.info("定时清理任务执行完成")

scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_tasks, 'cron', hour=3, minute=30)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

# ==================== 启动服务 ====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8123,
        log_level="info",
        proxy_headers=True,
        server_header=False
    )
