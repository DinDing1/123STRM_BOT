# ================ strm_core.py ================
import os
import re
import asyncio
import time
import requests
import threading
import sqlite3
import hashlib
import importlib
from p123.tool import share_iterdir
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from colorama import init, Fore, Style

# 初始化colorama
init(autoreset=True)

# ========================= 全局配置 =========================
class Config:
    TG_TOKEN = os.getenv("TG_TOKEN", "")
    USER_ID = int(os.getenv("USER_ID", ""))
    ADAPTER = os.getenv("ADAPTER", "telegram_bot,telegram_client")  # 修改为逗号分隔多个适配器
    ADMINS = list(map(int, os.getenv("ADMINS", "").split(",")))  # 管理员列表
    BASE_URL = os.getenv("BASE_URL", "")
    TG_API_ID = os.getenv("TG_API_ID", "")
    TG_API_HASH = os.getenv("TG_API_HASH", "")
    TG_SESSION = os.getenv("TG_SESSION", "/app/data/strm_client")
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")
    DB_PATH = os.getenv("DB_PATH", "/app/data/strm_records.db")
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts', '.mp3', '.flac')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt')
    MAX_DEPTH = -1

# ========================= 新增适配器接口 =========================
def load_adapters():
    """动态加载所有适配器（修复线程事件循环）"""
    adapters = [a.strip() for a in Config.ADAPTER.split(",") if a.strip()]
    
    for adapter in adapters:
        try:
            module = importlib.import_module(adapter)
            start_func = getattr(module, "start_adapter")
            
            # 为每个适配器创建独立线程和事件循环
            def adapter_runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                start_func()
            
            t = threading.Thread(target=adapter_runner, daemon=False)
            t.start()
            print(f"🚀 加载 {adapter} 适配器")
            
        except Exception as e:
            print(f"❌ 适配器 {adapter} 加载失败: {str(e)}")


# ========================= 新增工具函数 =========================
def format_duplicate_ids(ids):
    """格式化重复ID显示（专用于生成报告）"""
    if not ids:
        return "无"
    
    ids = sorted(set(ids))
    ranges = []
    start = end = ids[0]
    
    for current in ids[1:]:
        if current == end + 1:
            end = current
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = current
    ranges.append(f"{start}-{end}" if start != end else str(start))
    
    return ' '.join(ranges)

# ========================= 数据库操作 =========================
def init_db():
    with sqlite3.connect(Config.DB_PATH) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='strm_records'")
        if not cursor.fetchone():
            conn.execute('''CREATE TABLE strm_records
                         (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          file_name TEXT NOT NULL,
                          file_size INTEGER NOT NULL CHECK(file_size > 0),
                          md5 TEXT NOT NULL CHECK(length(md5) = 32),
                          s3_key_flag TEXT NOT NULL,
                          strm_path TEXT NOT NULL UNIQUE,
                          status INTEGER DEFAULT 1,
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        else:
            try:
                conn.execute("ALTER TABLE strm_records ADD COLUMN status INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass
        conn.commit()

def check_exists(file_size, md5, s3_key_flag):
    with sqlite3.connect(Config.DB_PATH) as conn:
        cursor = conn.execute('''SELECT id FROM strm_records 
                               WHERE file_size=? AND md5=? AND s3_key_flag=? AND status=1''',
                            (file_size, md5, s3_key_flag))
        return cursor.fetchone()

def add_record(file_name, file_size, md5, s3_key_flag, strm_path):
    with sqlite3.connect(Config.DB_PATH) as conn:
        cursor = conn.execute('''SELECT id FROM strm_records 
                               WHERE strm_path=? AND status=0''',
                            (strm_path,))
        if existing := cursor.fetchone():
            conn.execute('''UPDATE strm_records 
                          SET status=1, file_name=?, file_size=?, md5=?, s3_key_flag=?
                          WHERE id=?''',
                       (file_name, file_size, md5, s3_key_flag, existing[0]))
        else:
            conn.execute('''INSERT INTO strm_records 
                          (file_name, file_size, md5, s3_key_flag, strm_path)
                          VALUES (?, ?, ?, ?, ?)''',
                       (file_name, file_size, md5, s3_key_flag, strm_path))
        conn.commit()

# ========================= 核心功能 =========================
def delete_records(record_ids):
    with sqlite3.connect(Config.DB_PATH) as conn:
        try:
            placeholders = ','.join(['?'] * len(record_ids))
            cursor = conn.execute(
                f"UPDATE strm_records SET status=0 WHERE id IN ({placeholders})",
                record_ids
            )
            conn.commit()
            return cursor.rowcount
        except sqlite3.Error as e:
            print(f"数据库错误: {str(e)}")
            return 0

def get_deleted_ids(attempted_ids):
    with sqlite3.connect(Config.DB_PATH) as conn:
        placeholders = ','.join(['?'] * len(attempted_ids))
        cursor = conn.execute(
            f"SELECT id FROM strm_records WHERE id IN ({placeholders}) AND status=0",
            attempted_ids
        )
        return [row[0] for row in cursor.fetchall()]

def format_ids(ids):
    if len(ids) <= 10:
        return ', '.join(map(str, sorted(ids)))
    sorted_ids = sorted(ids)
    ranges = []
    start = end = sorted_ids[0]
    for current_id in sorted_ids[1:]:
        if current_id == end + 1:
            end = current_id
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = current_id
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ' '.join(ranges)

def clear_database():
    with sqlite3.connect(Config.DB_PATH) as conn:
        conn.execute("DELETE FROM strm_records")
        conn.commit()
        return True

def get_all_records():
    with sqlite3.connect(Config.DB_PATH) as conn:
        cursor = conn.execute("SELECT * FROM strm_records WHERE status=1")
        return cursor.fetchall()

def parse_strm_content(content):
    try:
        uri = content.strip()
        if not uri.startswith(("http://", "https://")):
            uri = "http://" + uri
        parsed = urlparse(uri)
        path_part = parsed.path.lstrip('/')
        query_part = parsed.query
        name_part, size_str, md5 = path_part.rsplit("|", 2)
        file_size = int(size_str)
        if len(md5) != 32 or not all(c in "0123456789abcdef" for c in md5):
            raise ValueError("Invalid MD5 hash")
        s3_key_flag = query_part.split("&")[0] if query_part else ""
        return {
            "name": unquote(name_part),
            "file_size": file_size,
            "md5": md5,
            "s3_key_flag": s3_key_flag
        }
    except Exception as e:
        raise ValueError(f"Invalid STRM content: {str(e)}")

def import_strm_files():
    counts = {'imported': 0, 'skipped': 0, 'invalid': 0, 'errors': 0}
    print(f"🚚 开始扫描STRM文件目录...")
    for root, _, files in os.walk(Config.OUTPUT_ROOT):
        for filename in files:
            if not filename.endswith('.strm'):
                continue
            strm_path = os.path.abspath(os.path.join(root, filename))
            try:
                with open(strm_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                data = parse_strm_content(content)
                if data['file_size'] <= 0 or not data['s3_key_flag']:
                    counts['invalid'] += 1
                    continue
                if check_exists(data['file_size'], data['md5'], data['s3_key_flag']):
                    counts['skipped'] += 1
                    continue
                try:
                    add_record(
                        file_name=data['name'],
                        file_size=data['file_size'],
                        md5=data['md5'],
                        s3_key_flag=data['s3_key_flag'],
                        strm_path=strm_path
                    )
                    counts['imported'] += 1
                except sqlite3.IntegrityError:
                    counts['skipped'] += 1
            except ValueError:
                counts['invalid'] += 1
            except Exception:
                counts['errors'] += 1
    return counts

def generate_strm_files(domain: str, share_key: str, share_pwd: str):
    counts = {
        'video': 0, 
        'subtitle': 0, 
        'error': 0, 
        'skipped': 0,
        'invalid': 0,
        'skipped_ids': []
    }

    print(f"{Fore.YELLOW}🚀 开始处理 {domain} 的分享：{share_key}")

    for info in share_iterdir(share_key, share_pwd, domain=domain,
                            max_depth=Config.MAX_DEPTH, predicate=lambda x: not x["is_dir"]):
        try:
            raw_uri = unquote(info["uri"].split("://", 1)[-1])
            relpath = info["relpath"]
            ext = os.path.splitext(relpath)[1].lower()

            if ext not in Config.VIDEO_EXTENSIONS + Config.SUBTITLE_EXTENSIONS:
                continue

            output_path = os.path.join(Config.OUTPUT_ROOT, relpath)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            if ext in Config.VIDEO_EXTENSIONS:
                try:
                    if "?" in raw_uri:
                        size_md5, s3_key_flag = raw_uri.split("?", 1)
                    else:
                        size_md5 = raw_uri
                        s3_key_flag = "unknown_" + hashlib.md5(raw_uri.encode()).hexdigest()[:8]

                    parts = size_md5.split("|")
                    if len(parts) >= 3:
                        name_part, size_str, md5 = parts[-3:]
                    else:
                        md5 = "invalid_" + hashlib.md5(size_md5.encode()).hexdigest()
                        size_str = parts[-1] if parts else "0"
                        name_part = os.path.basename(relpath).split('.')[0]

                    file_size = int(size_str) if size_str.isdigit() else 0
                    s3_key_flag = s3_key_flag.split("&")[0]

                    if file_size == 0 or md5.startswith("invalid"):
                        counts['invalid'] += 1
                        print(f"{Fore.RED}⚠️ 无效文件记录：{relpath}")
                        continue

                    strm_path = os.path.abspath(os.path.splitext(output_path)[0] + '.strm')

                    if existing := check_exists(file_size, md5, s3_key_flag):
                        counts['skipped'] += 1
                        counts['skipped_ids'].append(existing[0])
                        print(f"{Fore.CYAN}⏩ 跳过重复文件 [ID:{existing[0]}]: {relpath}")
                        continue

                    with open(strm_path, 'w', encoding='utf-8') as f:
                        f.write(f"{Config.BASE_URL}/{name_part}|{file_size}|{md5}?{s3_key_flag}")
                    
                    add_record(os.path.basename(relpath), file_size, md5, s3_key_flag, strm_path)
                    counts['video'] += 1
                    print(f"{Fore.GREEN}✅ 视频文件：{relpath}")
                except Exception as e:
                    print(f"{Fore.RED}❌ 处理失败：{relpath}\n{str(e)}")

                except sqlite3.IntegrityError as e:
                    counts['error'] += 1
                    print(f"{Fore.RED}❌ 数据库冲突：{relpath}\n{str(e)}")
                except Exception as parse_error:
                    counts['error'] += 1
                    print(f"{Fore.RED}❌ 处理失败：{relpath}\n{str(parse_error)}")

            elif ext in Config.SUBTITLE_EXTENSIONS:
                try:
                    download_url = f"https://{domain}/{raw_uri}"
                    response = requests.get(
                        download_url,
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': f'https://{domain}/'},
                        timeout=20
                    )
                    response.raise_for_status()
                    
                    with open(output_path, 'wb') as f:
                        f.write(response.content)
                    counts['subtitle'] += 1
                    print(f"{Fore.BLUE}📝 字幕文件：{relpath}")
                except Exception as e:
                    print(f"{Fore.RED}❌ 下载失败：{relpath}\n{str(e)}")

        except Exception as e:
            print(f"{Fore.RED}❌ 全局异常：{relpath}\n{str(e)}")
    
    return counts

# ========================= 修改后的主程序入口 =========================
if __name__ == "__main__":
    init_db()
    os.makedirs(Config.OUTPUT_ROOT, exist_ok=True)
    load_adapters()
    
    # 保持主线程存活
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n🛑 收到终止信号，程序退出")
