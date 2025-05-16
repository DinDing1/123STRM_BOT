import os
import re
import sqlite3
import requests
import hashlib
import asyncio
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
from httpx import AsyncClient, Limits
from telegram.error import NetworkError

# 初始化colorama（控制台彩色输出）
init(autoreset=True)

# 对话状态
CONFIRM_CLEAR = 1

# ========================= 全局配置 =========================
class Config:
    TG_TOKEN = os.getenv("TG_TOKEN", "")     # Telegram机器人令牌
    USER_ID = int(os.getenv("USER_ID", ""))  # 授权用户ID
    BASE_URL = os.getenv("BASE_URL", "")    # STRM文件指向的基础URL
    PROXY_URL = os.getenv("PROXY_URL", "")   # 代理地址（可选）
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")# STRM文件输出目录
    DB_PATH = os.getenv("DB_PATH", "/app/data/strm_records.db") # 数据库文件路径
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts', '.mp3', '.flac')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt') # 支持的字幕扩展名
    MAX_DEPTH = -1 # 目录遍历深度限制（-1表示无限制）
# ========================= 权限控制装饰器 =========================
# 权限验证装饰器（静默模式）
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != Config.USER_ID:
            return  # 未授权用户，直接返回，不进行任何响应
        return await func(update, context)
    return wrapped
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
    """批量删除记录"""
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
    """查询实际被删除的有效ID"""
    with sqlite3.connect(Config.DB_PATH) as conn:
        placeholders = ','.join(['?'] * len(attempted_ids))
        cursor = conn.execute(
            f"SELECT id FROM strm_records WHERE id IN ({placeholders}) AND status=0",
            attempted_ids
        )
        return [row[0] for row in cursor.fetchall()]

def format_ids(ids):
    """格式化ID显示（超过10个用区间表示）"""
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

# ========================= Telegram处理器 =========================

def format_duplicate_ids(ids):
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
    
    merged_ranges = []
    for r in ranges:
        if '-' in r:
            s, e = map(int, r.split('-'))
            if merged_ranges and '-' in merged_ranges[-1]:
                last_s, last_e = map(int, merged_ranges[-1].split('-'))
                if last_e + 1 == s:
                    merged_ranges[-1] = f"{last_s}-{e}"
                    continue
            merged_ranges.append(r)
        else:
            merged_ranges.append(r)
    
    return ' '.join(merged_ranges) if merged_ranges else "无"

@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理123网盘链接"""
    msg = update.message.text
    # 匹配分享链接格式
    pattern = r'(https?://[^\s/]+/s/)([a-zA-Z0-9\-_]+)(?:[\s\S]*?(?:提取码|密码|code)[\s:：=]*(\w{4}))?'    
    if not (match := re.search(pattern, msg, re.IGNORECASE)):
        return
    
    domain = urlparse(match.group(1)).netloc
    share_key = match.group(2)
    share_pwd = match.group(3) or ""

    await update.message.reply_text(f"🔄 开始生成 {share_key} 的STRM...")

    try:
        if not re.match(r'^[a-zA-Z0-9\-_]+$', share_key):
            raise ValueError(f"无效分享码格式：{share_key}")
            
        start_time = datetime.now()
        report = generate_strm_files(domain, share_key, share_pwd)
        id_ranges = format_duplicate_ids(report['skipped_ids'])
        
        result_msg = (
            f"✅ 处理完成！\n"
            f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒\n"
            f"🎬 视频: {report['video']} | 📝 字幕: {report['subtitle']}\n"
            f"⏩ 跳过重复: {report['skipped']} | 重复ID: {id_ranges}"
        )
        if report['invalid']:
            result_msg += f"\n⚠️ 无效记录: {report['invalid']}个"
        if report['error']:
            result_msg += f"\n❌ 处理错误: {report['error']}个"
            
        await update.message.reply_text(result_msg)
    except Exception as e:
        await update.message.reply_text(f"❌ 处理失败：{str(e)}")

@restricted
async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理删除命令，支持批量ID和区间"""
    if not context.args:
        await update.message.reply_text(
            "❌ 用法示例：\n"
            "单个ID：/delete 664\n"
            "多个ID：/delete 664 665 667\n"
            "区间ID：/delete 664-670\n"
            "混合模式：/delete 664-670 675 680-685"
        )
        return

    raw_ids = []
    for arg in context.args:
        if '-' in arg:
            try:
                start, end = sorted(map(int, arg.split('-')))
                raw_ids.extend(range(start, end + 1))
            except:
                await update.message.reply_text(f"❌ 无效区间格式：{arg}")
                return
        else:
            try:
                raw_ids.append(int(arg))
            except:
                await update.message.reply_text(f"❌ 无效ID格式：{arg}")
                return

    unique_ids = list({x for x in raw_ids if x > 0})
    if not unique_ids:
        await update.message.reply_text("⚠️ 未提供有效ID")
        return

    try:
        deleted_count = delete_records(unique_ids)
        failed_count = len(unique_ids) - deleted_count
        
        result = [
            f"🗑️ 请求删除：{len(unique_ids)} 个记录",
            f"✅ 成功删除：{deleted_count} 个",
            f"❌ 未找到记录：{failed_count} 个"
        ]
        
        if deleted_count > 0:
            success_ids = get_deleted_ids(unique_ids)
            result.append(f"成功ID：{format_ids(success_ids)}")
            
        if failed_count > 0:
            failed_ids = list(set(unique_ids) - set(success_ids))
            result.append(f"失败ID：{format_ids(failed_ids)}")

        await update.message.reply_text('\n'.join(result))
        
    except Exception as e:
        await update.message.reply_text(f"❌ 删除操作异常：{str(e)}")

@restricted
async def handle_clear_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚠️ 确认要清空所有数据库记录吗？此操作不可恢复！\n"
        "请回复'确认清空'继续操作，或回复任意内容取消"
    )
    return CONFIRM_CLEAR

@restricted
async def handle_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == '确认清空':
        if clear_database():
            await update.message.reply_text("✅ 数据库已成功清空")
        else:
            await update.message.reply_text("❌ 清空数据库失败")
    else:
        await update.message.reply_text("❌ 已取消清空操作")
    return ConversationHandler.END

@restricted
async def cancel_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 已取消清空操作")
    return ConversationHandler.END

@restricted
async def handle_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        success = 0
        failed = 0
        records = get_all_records()
        
        if not records:
            await update.message.reply_text("⚠️ 数据库中没有可恢复的记录")
            return

        await update.message.reply_text(f"🔄 开始恢复 {len(records)} 个STRM文件...")
        
        for record in records:
            try:
                strm_path = Path(record[5])
                uri = f"{Config.BASE_URL}/{record[1]}|{record[2]}|{record[3]}?{record[4]}"
                
                if strm_path.exists():
                    continue
                
                strm_path.parent.mkdir(parents=True, exist_ok=True)
                with open(strm_path, 'w') as f:
                    f.write(uri)
                success += 1
                
            except Exception as e:
                print(f"恢复失败 ID:{record[0]} {str(e)}")
                failed += 1

        result_msg = (
            f"✅ 恢复完成\n"
            f"成功恢复: {success} 个\n"
            f"恢复失败: {failed} 个"
        )
        await update.message.reply_text(result_msg)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 恢复失败：{str(e)}")

@restricted
async def handle_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        start_time = datetime.now()
        report = import_strm_files()
        
        result_msg = (
            f"📦 导入完成！\n"
            f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒\n"
            f"🆕 新增记录: {report['imported']}\n"
            f"⏩ 跳过记录: {report['skipped']}\n"
            f"⚠️ 无效文件: {report['invalid']}\n"
            f"❌ 处理错误: {report['errors']}"
        )
        await update.message.reply_text(result_msg)
    except Exception as e:
        await update.message.reply_text(f"❌ 导入失败：{str(e)}")

async def post_init(application: Application):
    commands = [
        BotCommand("delete", "删除指定ID的记录"),
        BotCommand("clear", "清空数据库记录"),
        BotCommand("restore", "恢复STRM文件到本地"),
        BotCommand("import", "导入STRM文件到数据库")
    ]
    await application.bot.set_my_commands(commands)
    print(f"{Fore.CYAN}📱 Telegram菜单已加载")

# ========================= 主程序入口 =========================
# ========================= 主程序入口 =========================
if __name__ == "__main__":
    init_db()
    os.makedirs(Config.OUTPUT_ROOT, exist_ok=True)
    
    # 创建自定义请求配置（修改后）
    from httpx import Limits
    request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=30.0,
        read_timeout=30.0,
        proxy=Config.PROXY_URL if Config.PROXY_URL else None,
        retries=3,
        limits=Limits(max_keepalive_connections=50, max_connections=100)
    )
    
    builder = (
        Application.builder()
        .token(Config.TG_TOKEN)
        .post_init(post_init)
        .get_updates_request(request)
        .connect_timeout(60.0)
        .read_timeout(60.0)
    )
    
    # 添加全局错误处理（新增）
    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if isinstance(context.error, NetworkError):
            logger.error(f"网络连接异常: {context.error}, 10秒后自动重连...")
            await asyncio.sleep(10)
            await app.start()
        else:
            logger.error(f"未处理的异常: {context.error}")

    app = builder.build()
    app.add_error_handler(error_handler)
    
    # 添加会话处理器
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("clear", handle_clear_start)],
        states={
            CONFIRM_CLEAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clear_confirm)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_clear)],
    )
    # 注册所有处理器    
    app.add_handler(CommandHandler("delete", handle_delete))
    app.add_handler(CommandHandler("restore", handle_restore))
    app.add_handler(CommandHandler("import", handle_import))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(
    filters.TEXT & 
    ~filters.COMMAND & 
    filters.Regex(r'https?://[^\s/]+/s/[a-zA-Z0-9\-_]+'),
    handle_message
))

    #print(f"{Fore.GREEN}🤖 TG机器人已启动 | 数据库：{Config.DB_PATH} | STRM输出目录：{os.path.abspath(Config.OUTPUT_ROOT)} ")
    app.run_polling()
