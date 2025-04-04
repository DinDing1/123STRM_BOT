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
import base64
from urllib.parse import urlparse, parse_qs, urlencode, unquote
import random

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
    "   123网盘直连服务 (VIP破解增强版)\n\n"
    "                      版本号：{}\n"
    "-----------------------------------------".format(VERSION)
)

def modify_download_url(original_url: str) -> str:
    """深度链接修改逻辑"""
    try:
        parsed = urlparse(original_url)
        
        # 处理web-pro域名的情况
        if "web-pro" in parsed.netloc:
            params = parsed.query.split("params=")[1]
            decoded = unquote(base64.b64decode(params).decode())
            
            # 添加破解参数
            new_params = urlparse(decoded)
            query = parse_qs(new_params.query)
            query.update({
                "auto_redirect": ["0"],
                "speed_limit": ["0"],
                "vip_channel": ["1"]
            })
            
            # 重新构建参数
            new_query = urlencode(query, doseq=True)
            modified = new_params._replace(query=new_query).geturl()
            return parsed._replace(query=f"params={base64.b64encode(modified.encode()).decode()}").geturl()
            
        # 处理其他域名的情况
        else:
            query = parse_qs(parsed.query)
            query.update({
                "auto_redirect": ["0"],
                "speed_limit": ["0"], 
                "vip_channel": ["1"]
            })
            return parsed._replace(query=urlencode(query, doseq=True)).geturl()
            
    except Exception as e:
        logger.warning(f"链接修改失败: {str(e)}，使用原始链接")
        return original_url

class PatchedP123Client(P123Client):
    """深度破解客户端"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage_quota = int(os.getenv("P123_STORAGE_QUOTA", 2099511627776))  # 默认1TB
        self.storage_used = int(os.getenv("P123_STORAGE_USED", 10048576))  # 默认已用1MB
        self.vip_expire = int(os.getenv("P123_VIP_EXPIRE", 253402185600))  # 2099年过期
    
    def _request_headers(self) -> dict:
        base_headers = super()._request_headers()
        base_headers.update({
            "user-agent": "123pan/v2.4.7 (Android_13.0;Xiaomi 14 Ultra)",
            "platform": "android",
            "app-version": "69",
            "x-app-version": "2.4.7",
            "x-vip-status": "2",
            "x-vip-expire": str(self.vip_expire),
            "x-storage-quota": str(self.storage_quota),
            "x-storage-used": str(self._get_dynamic_used_space()),
            "x-device-id": "38DBA3E9-1234-5678-9ABC-DEF123456789"
        })
        return base_headers
    
    def _get_dynamic_used_space(self):
        """生成动态使用空间数据"""
        base_used = self.storage_used
        fluctuation = random.randint(-51200, 51200)  # ±50KB波动
        return max(0, base_used + fluctuation)

    def user_info(self):
        """重写用户信息获取"""
        original_info = super().user_info()
        if isinstance(original_info, dict) and original_info.get("code") == 200:
            # 修改关键信息
            original_info["data"].update({
                "Vip": True,
                "VipLevel": 2,
                "TotalSize": self.storage_quota,
                "UsedSize": self._get_dynamic_used_space(),
                "IsShowAdvertisement": False,
                "UserVipDetailInfos": [{
                    "VipDesc": "SVIP 会员",
                    "TimeDesc": "2099-12-31 到期",
                    "IsUse": True
                }]
            })
        return original_info

    def download_info(self, payload):
        """增强版下载信息获取"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = super().download_info(payload)
                if isinstance(response, dict) and response.get("data"):
                    # 修改下载链接
                    if "DownloadUrl" in response["data"]:
                        response["data"]["DownloadUrl"] = modify_download_url(response["data"]["DownloadUrl"])
                    if "DownloadURL" in response["data"]:
                        response["data"]["DownloadURL"] = modify_download_url(response["data"]["DownloadURL"])
                    # 更新虚拟存储空间
                    self._update_virtual_storage(payload.get("Size", 0))
                return response
            except P123OSError as e:
                if "空间已满" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"触发虚拟空间重置 (尝试 {attempt+1}/{max_retries})")
                    self._reset_virtual_storage()
                    continue
                raise
    
    def _update_virtual_storage(self, file_size: int):
        """更新虚拟存储空间"""
        self.storage_used = min(
            self.storage_quota,
            self.storage_used + int(file_size * 0.1)  # 实际只记录10%大小
        logger.debug(f"虚拟存储更新: 已用空间 {self.storage_used} bytes")

    def _reset_virtual_storage(self):
        """重置虚拟存储空间"""
        self.storage_used = int(os.getenv("P123_STORAGE_USED", 1048576))
        logger.warning("虚拟存储空间已重置")

# 初始化客户端
client = PatchedP123Client(
    passport=os.getenv("P123_PASSPORT"),
    password=os.getenv("P123_PASSWORD")
)
token_expiry = None

# SQLite 缓存数据库路径
DB_DIR = "/app/data"
DB_PATH = os.path.join(DB_DIR, "cache.db")

def init_db():
    """初始化数据库"""
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

# 定时任务配置
scheduler = BackgroundScheduler()
scheduler.add_job(clear_all_cache, 'interval', hours=48)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

def login_client():
    """增强登录逻辑"""
    global client, token_expiry
    max_retries = 3
    for attempt in range(max_retries):
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
                return
            else:
                logger.error(f"登录失败: {login_response}")
                if attempt < max_retries - 1:
                    logger.info(f"登录重试中 ({attempt+1}/{max_retries})")
                    time.sleep(2 ** attempt)
                    continue
                raise P123OSError(errno.EIO, login_response)
        except Exception as e:
            logger.error(f"登录错误: {str(e)}")
            if attempt == max_retries - 1:
                raise

def ensure_token_valid():
    """增强Token验证"""
    global token_expiry
    if token_expiry is None:
        logger.info("Token未初始化，正在重新登录...")
        login_client()
    else:
        token_expiry_naive = token_expiry.replace(tzinfo=None)
        if datetime.now() >= token_expiry_naive - timedelta(minutes=5):  # 提前5分钟续期
            logger.info("Token即将过期，主动续期...")
            login_client()

login_client()

app = FastAPI(debug=False)

@app.get("/{uri:path}")
@app.head("/{uri:path}")
async def index(request: Request, uri: str):
    try:
        logger.info(f"收到请求: {request.url}")

        ensure_token_valid()

        if uri.count("|") < 2:
            return JSONResponse({"state": False, "message": "URI格式错误"}, 400)

        parts = uri.split("|")
        file_name, size_str, etag_part = parts[0], parts[1], parts[2]
        try:
            size = int(size_str)
            etag = etag_part.split("?")[0]
        except ValueError:
            return JSONResponse({"state": False, "message": "参数类型错误"}, 400)

        s3_key_flag = request.query_params.get("s3keyflag", "")

        clear_expired_entries()

        # 缓存查询
        with closing(sqlite3.connect(DB_PATH)) as conn:
            c = conn.cursor()
            c.execute('''SELECT download_url FROM cache 
                      WHERE file_name=? AND size=? AND etag=? 
                      AND expires_at > datetime('now')''',
                      (file_name, size, etag))
            if (row := c.fetchone()):
                logger.info(f"缓存命中: {file_name}")
                return RedirectResponse(row[0], 302)

        # 处理下载请求
        payload = {"FileName": file_name, "Size": size, "Etag": etag, "S3KeyFlag": s3_key_flag}
        
        try:
            download_resp = check_response(client.download_info(payload))
        except P123OSError as e:
            if "空间已满" in str(e):
                logger.warning("触发虚拟空间重置机制")
                client._reset_virtual_storage()
                download_resp = check_response(client.download_info(payload))
            else:
                raise

        download_url = download_resp["data"].get("DownloadUrl") or download_resp["data"].get("DownloadURL")

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
