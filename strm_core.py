import os
import re
import sqlite3
import requests
import hashlib
import asyncio #新增依赖
from p123.tool import share_iterdir
from datetime import datetime
from colorama import init, Fore, Style
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    ConversationHandler,
)
from urllib.parse import unquote, urlparse
from pathlib import Path
from telegram.request import HTTPXRequest
from telethon import TelegramClient, events #新增依赖
from typing import Union, List #新增依赖

# 初始化colorama
init(autoreset=True)

# 对话状态
CONFIRM_CLEAR = 1

# ========================= 全局配置 =========================
class Config:
    # Bot配置
    TG_TOKEN = os.getenv("TG_TOKEN", "")
    USER_ID = int(os.getenv("USER_ID", ""))
    
    # 用户模式配置
    TG_API_ID = os.getenv("TG_API_ID", "")
    TG_API_HASH = os.getenv("TG_API_HASH", "")
    TG_SESSION = os.getenv("TG_SESSION", "/app/data/userbot")
    ADMINS = list(map(int, os.getenv("ADMINS", "").split()))  # 添加您的号码ID
    
    # 通用配置
    BASE_URL = os.getenv("BASE_URL", "http://10.10.10.11:8123")
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")
    DB_PATH = os.getenv("DB_PATH", "/app/data/strm_records.db")
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts', '.mp3', '.flac')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt')
    MAX_DEPTH = -1

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
    counts = {
        'imported': 0,
        'skipped': 0,
        'invalid': 0,
        'errors': 0
    }

    print(f"{Fore.YELLOW}🚚 开始扫描STRM文件目录...")
    
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
                    print(f"{Fore.RED}⚠️ 无效记录：{strm_path}")
                    continue
                
                if check_exists(data['file_size'], data['md5'], data['s3_key_flag']):
                    counts['skipped'] += 1
                    print(f"{Fore.CYAN}⏩ 跳过已存在记录：{strm_path}")
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
                    print(f"{Fore.GREEN}✅ 导入成功：{strm_path}")
                except sqlite3.IntegrityError:
                    counts['skipped'] += 1
                    print(f"{Fore.CYAN}⏩ 路径冲突：{strm_path}")
                
            except ValueError as e:
                counts['invalid'] += 1
                print(f"{Fore.RED}⚠️ 解析失败：{strm_path}\n{str(e)}")
            except Exception as e:
                counts['errors'] += 1
                print(f"{Fore.RED}❌ 处理异常：{strm_path}\n{str(e)}")
    
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

                except sqlite3.IntegrityError as e:
                    counts['error'] += 1
                    print(f"{Fore.RED}❌ 数据库冲突：{relpath}\n{str(e)}")
                except Exception as parse_error:
                    counts['error'] += 1
                    print(f"{Fore.RED}❌ 处理失败：{relpath}\n{str(parse_error)}")

            elif ext in Config.SUBTITLE_EXTENSIONS:
                try:
                    download_url = f"https://{domain}/{raw_uri}"
                    for retry in range(3):
                        try:
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
                            break
                        except Exception:
                            if retry == 2:
                                counts['error'] += 1
                                print(f"{Fore.RED}❌ 下载失败：{relpath}")
                except Exception as e:
                    counts['error'] += 1
                    print(f"{Fore.RED}❌ 字幕处理失败：{relpath}\n{str(e)}")

        except Exception as e:
            counts['error'] += 1
            print(f"{Fore.RED}❌ 全局异常：{relpath}\n{str(e)}")
    
    return counts

# ========================= 权限控制装饰器 =========================
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != Config.USER_ID:
            return
        return await func(update, context)
    return wrapped

def user_restricted(func):
    async def wrapped(event):
        user_id = event.sender_id
        if user_id not in Config.ADMINS:
            return
        return await func(event)
    return wrapped

# ========================= 核心服务层 =========================
def format_duplicate_ids(ids):
    """格式化重复ID的显示（合并连续ID为区间）"""
    if not ids:
        return "无"
    
    sorted_ids = sorted(set(ids))
    ranges = []
    start = end = sorted_ids[0]
    
    for current in sorted_ids[1:]:
        if current == end + 1:
            end = current
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = current
    ranges.append(f"{start}-{end}" if start != end else str(start))
    
    return ' '.join(ranges) if ranges else "无"

class CoreService:
    @staticmethod
    async def process_message(link: str) -> str:
        pattern = r'(https?://[^\s/]+/s/)([a-zA-Z0-9\-_]+)(?:[\s\S]*?(?:提取码|密码|code)[\s:：=]*(\w{4}))?'
        if not (match := re.search(pattern, link, re.IGNORECASE)):
            return "❌ 无效的分享链接格式"

        domain = urlparse(match.group(1)).netloc
        share_key = match.group(2)
        share_pwd = match.group(3) or ""

        try:
            start_time = datetime.now()
            report = generate_strm_files(domain, share_key, share_pwd)
            id_ranges = format_duplicate_ids(report['skipped_ids'])
            
            result = [
                f"✅ 处理完成！",
                f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒",
                f"🎬 视频: {report['video']} | 📝 字幕: {report['subtitle']}",
                f"⏩ 跳过: {report['skipped']} | 重复ID: {id_ranges}"
            ]
            if report['invalid']: result.append(f"⚠️ 无效记录: {report['invalid']}个")
            if report['error']: result.append(f"❌ 错误: {report['error']}个")
            
            return '\n'.join(result)
        except Exception as e:
            return f"❌ 处理失败：{str(e)}"

    @staticmethod
    def process_delete(ids: List[int]) -> dict:
        deleted_count = delete_records(ids)
        success_ids = get_deleted_ids(ids)
        failed_ids = list(set(ids) - set(success_ids))
        
        return {
            'total': len(ids),
            'success': deleted_count,
            'failed': len(failed_ids),
            'success_ids': success_ids,
            'failed_ids': failed_ids
        }

    @staticmethod
    def process_clear() -> bool:
        return clear_database()

    @staticmethod
    def process_restore() -> dict:
        records = get_all_records()
        if not records:
            return {'total': 0, 'success': 0, 'failed': 0}
        
        success = 0
        failed = 0
        for record in records:
            try:
                strm_path = Path(record[5])
                if strm_path.exists():
                    continue
                    
                strm_path.parent.mkdir(parents=True, exist_ok=True)
                with open(strm_path, 'w') as f:
                    f.write(f"{Config.BASE_URL}/{record[1]}|{record[2]}|{record[3]}?{record[4]}")
                success += 1
            except:
                failed += 1
                
        return {'total': len(records), 'success': success, 'failed': failed}

    @staticmethod
    def process_import() -> dict:
        start_time = datetime.now()
        report = import_strm_files()
        return {
            'time': (datetime.now() - start_time).total_seconds(),
            **report
        }

# ========================= 接口适配层 =========================
class BotAdapter:
    """Telegram Bot适配器"""
    @staticmethod
    def restricted(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if update.effective_user.id != Config.USER_ID:
                return
            return await func(update, context)
        return wrapper

    @staticmethod
    async def send_reply(target: Union[Update, events.NewMessage], message: str):
        """统一回复接口"""
        if isinstance(target, Update):
            await target.message.reply_text(message)
        else:
            await target.reply(message)

    @classmethod
    async def handle_delete(cls, target: Union[Update, events.NewMessage], args: list):
        """删除命令适配"""
        if not args:
            await cls.send_reply(target, "❌ 参数错误！使用示例：/delete 123 456-789")
            return

        raw_ids = []
        for arg in args:
            if '-' in arg:
                try:
                    start, end = sorted(map(int, arg.split('-')))
                    raw_ids.extend(range(start, end + 1))
                except:
                    await cls.send_reply(target, f"❌ 无效区间格式：{arg}")
                    return
            else:
                try:
                    raw_ids.append(int(arg))
                except:
                    await cls.send_reply(target, f"❌ 无效ID格式：{arg}")
                    return

        unique_ids = list({x for x in raw_ids if x > 0})
        if not unique_ids:
            await cls.send_reply(target, "⚠️ 未提供有效ID")
            return

        result = CoreService.process_delete(unique_ids)
        response = [
            f"🗑️ 请求删除：{result['total']}",
            f"✅ 成功：{result['success']} | ❌ 失败：{result['failed']}"
        ]
        if result['success_ids']:
            response.append(f"成功ID：{format_ids(result['success_ids'])}")
        if result['failed_ids']:
            response.append(f"失败ID：{format_ids(result['failed_ids'])}")
            
        await cls.send_reply(target, '\n'.join(response))

    @classmethod
    async def handle_clear(cls, target: Union[Update, events.NewMessage]):
        """清空操作适配"""
        if CoreService.process_clear():
            await cls.send_reply(target, "✅ 数据库已清空")
        else:
            await cls.send_reply(target, "❌ 清空操作失败")

    @classmethod
    async def handle_restore(cls, target: Union[Update, events.NewMessage]):
        """恢复操作适配"""
        result = CoreService.process_restore()
        await cls.send_reply(target, 
            f"✅ 恢复完成\n成功: {result['success']} | 失败: {result['failed']}\n"
            f"总记录: {result['total']}"
        )

    @classmethod
    async def handle_import(cls, target: Union[Update, events.NewMessage]):
        """导入操作适配"""
        result = CoreService.process_import()
        response = [
            f"📦 导入完成！耗时: {result['time']:.1f}秒",
            f"🆕 新增: {result['imported']} | ⏩ 跳过: {result['skipped']}",
            f"⚠️ 无效: {result['invalid']} | ❌ 错误: {result['errors']}"
        ]
        await cls.send_reply(target, '\n'.join(response))

# ================ Telegram Bot实现 ================
@BotAdapter.restricted
async def bot_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot消息处理器"""
    result = await CoreService.process_message(update.message.text)
    await BotAdapter.send_reply(update, result)

@BotAdapter.restricted
async def bot_delete_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await BotAdapter.handle_delete(update, context.args)

@BotAdapter.restricted
async def bot_clear_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚠️ 确认清空数据库？回复'确认清空'继续")
    return CONFIRM_CLEAR

@BotAdapter.restricted
async def bot_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == '确认清空':
        await BotAdapter.handle_clear(update)
    else:
        await update.message.reply_text("❌ 操作取消")
    return ConversationHandler.END

# ================ Telegram用户客户端实现 ================
@user_restricted
async def user_message_handler(event: events.NewMessage.Event):
    """用户客户端消息处理器"""
    if event.text.startswith('/'):
        return
    
    result = await CoreService.process_message(event.text)
    await BotAdapter.send_reply(event, result)

@user_restricted
async def user_command_handler(event: events.NewMessage.Event):
    """用户客户端命令路由"""
    parts = event.text.split()
    if not parts:
        return
    
    cmd = parts[0]
    args = parts[1:] if len(parts) > 1 else []
    
    handlers = {
        '/delete': lambda: BotAdapter.handle_delete(event, args),
        '/clear': lambda: BotAdapter.handle_clear(event),
        '/restore': lambda: BotAdapter.handle_restore(event),
        '/import': lambda: BotAdapter.handle_import(event)
    }
    
    if cmd in handlers:
        try:
            await handlers[cmd]()
        except Exception as e:
            await event.reply(f"❌ 命令执行失败: {str(e)}")

# ================ 初始化函数 ================
async def post_init(application: Application):
    """Bot命令菜单初始化"""
    commands = [
        BotCommand("delete", "删除指定ID的记录"),
        BotCommand("clear", "清空数据库记录"),
        BotCommand("restore", "恢复STRM文件到本地"),
        BotCommand("import", "导入STRM文件到数据库")
    ]
    await application.bot.set_my_commands(commands)
    print(f"{Fore.CYAN}📱 Telegram菜单已加载")

async def start_bot():
    """独立运行Bot服务"""
    app = Application.builder() \
        .token(Config.TG_TOKEN) \
        .post_init(post_init) \
        .get_updates_request(HTTPXRequest(
            connect_timeout=60,
            read_timeout=60
        )).build()

    # 注册处理器
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("clear", bot_clear_start)],
        states={
            CONFIRM_CLEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_clear_confirm)]
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("delete", bot_delete_handler))
    app.add_handler(CommandHandler("restore", lambda u,c: BotAdapter.handle_restore(u)))
    app.add_handler(CommandHandler("import", lambda u,c: BotAdapter.handle_import(u)))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(r'https?://[^\s/]+/s/[a-zA-Z0-9\-_]+'),
        bot_message_handler
    ))
    
    try:
        print(f"{Fore.CYAN}🔄 Bot服务运行中...")
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        # 保持运行直到被取消
        while True:
            await asyncio.sleep(1)
            
    except asyncio.CancelledError:
        print(f"{Fore.YELLOW}🔄 正在停止Bot服务...")
    finally:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            print(f"{Fore.RED}⚠️ Bot关闭异常: {str(e)}")

async def start_user_client():
    """独立运行用户客户端"""
    client = TelegramClient(
        Config.TG_SESSION,
        Config.TG_API_ID,
        Config.TG_API_HASH,
        base_logger="telethon.client"
    )
    
    client.add_event_handler(
        user_message_handler,
        events.NewMessage(
            func=lambda e: not e.text.startswith('/') and 
            re.search(r'https?://[^\s/]+/s/[a-zA-Z0-9\-_]+', e.text or '')
        )
    )
    client.add_event_handler(
        user_command_handler,
        events.NewMessage(pattern=r'^/(delete|clear|restore|import)\b')
    )
    
    try:
        print(f"{Fore.CYAN}🔄 用户客户端运行中...")
        await client.start()
        
        # 保持运行直到被取消
        while True:
            await asyncio.sleep(1)
            
    except asyncio.CancelledError:
        print(f"{Fore.YELLOW}🔄 正在停止用户客户端...")
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception as e:
            print(f"{Fore.RED}⚠️ 客户端关闭异常: {str(e)}")
        raise
# ================ 修改主入口部分 ================
async def run_services():
    """协程方式运行服务"""
    bot_task = None
    client_task = None
    
    try:
        if Config.TG_TOKEN:
            bot_task = asyncio.create_task(start_bot())
            print(f"{Fore.GREEN}🤖 Bot服务已启动")
        
        if Config.TG_API_ID and Config.TG_API_HASH:
            client_task = asyncio.create_task(start_user_client())
            print(f"{Fore.GREEN}👤 用户客户端已启动")
        
        await asyncio.gather(
            *(task for task in [bot_task, client_task] if task is not None),
            return_exceptions=True
        )
    except asyncio.CancelledError:
        print(f"{Fore.YELLOW}🛑 正在停止服务...")
        if bot_task: bot_task.cancel()
        if client_task: client_task.cancel()
        await asyncio.sleep(1)  # 给任务结束时间

# ================ 主入口重构 ================
if __name__ == "__main__":
    # Windows系统特殊设置
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # 初始化配置和服务
    init_db()
    os.makedirs(Config.OUTPUT_ROOT, exist_ok=True)

    # 创建和管理事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        main_task = loop.create_task(run_services())
        loop.run_forever()
        
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}🛑 检测到Ctrl+C，正在关闭...")
    except Exception as e:
        print(f"{Fore.RED}❌ 主循环异常: {str(e)}")
    finally:
        # 安全关闭流程
        if not main_task.done():
            main_task.cancel()
            loop.run_until_complete(main_task)
        
        # 清理所有待处理任务
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        print(f"{Fore.RED}🚪 程序已完全退出")
