import os
import re
import asyncio
import requests
from pathlib import Path
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
from telegram.request import HTTPXRequest
from urllib.parse import urlparse
from strm_core import (
    Config,
    format_duplicate_ids,
    generate_strm_files,
    import_strm_files,
    get_all_records,
    delete_records,
    get_deleted_ids,
    format_ids,
    clear_database,
    init_db
)

# 初始化colorama
init(autoreset=True)

# ========================= 全局常量 =========================
CONFIRM_CLEAR = 1  # 清空确认对话状态

# ========================= 权限装饰器 =========================
def restricted(func):
    """仅允许授权用户操作"""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != Config.USER_ID:
            return
        return await func(update, context)
    return wrapped

# ========================= 消息处理器 =========================
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
    
        # 添加详细启动日志
    await update.message.reply_text(f"🔄 正在生成 {share_key} 的STRM文件...")

    try:
        start_time = datetime.now()
        report = generate_strm_files(domain, share_key, share_pwd)
        id_ranges = format_duplicate_ids(report['skipped_ids'])
        
        # 构建结果消息
        result_msg = (
            f"✅ 处理完成！\n"
            f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒\n"
            f"🎬 视频文件: {report['video']} | 📝 字幕文件: {report['subtitle']}\n"
            f"⏩ 跳过重复: {report['skipped']} | 重复ID范围: {id_ranges}"
        )
        if report['invalid'] > 0:
            result_msg += f"\n⚠️ 无效记录: {report['invalid']}个"
        if report['error'] > 0:
            result_msg += f"\n❌ 处理错误: {report['error']}个"
            
        await update.message.reply_text(result_msg)
    except Exception as e:
        print(f"{Fore.RED}❌ 处理失败：{str(e)}")
        await update.message.reply_text(f"❌ 处理失败：{str(e)}")

# ========================= 命令处理器 =========================
@restricted
async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """删除指定ID记录"""
    if not context.args:
        help_text = (
            "❌ 参数错误！使用方法：\n"
            "单个ID：/delete 123\n"
            "多个ID：/delete 123 456 789\n"
            "区间ID：/delete 100-200\n"
            "混合模式：/delete 100-200 250 300-350"
        )
        await update.message.reply_text(help_text)
        return

    raw_ids = []
    for arg in context.args:
        if '-' in arg:
            try:
                start, end = map(int, arg.split('-'))
                raw_ids.extend(range(min(start, end), max(start, end) + 1))
            except ValueError:
                await update.message.reply_text(f"❌ 无效区间格式：{arg}")
                return
        else:
            try:
                raw_ids.append(int(arg))
            except ValueError:
                await update.message.reply_text(f"❌ 无效ID格式：{arg}")
                return

    unique_ids = list(set(filter(lambda x: x > 0, raw_ids)))
    if not unique_ids:
        await update.message.reply_text("⚠️ 未提供有效ID")
        return

    try:
        deleted_count = delete_records(unique_ids)
        success_ids = get_deleted_ids(unique_ids)
        failed_ids = list(set(unique_ids) - set(success_ids))
        
        result_msg = (
            f"🗑️ 请求删除ID数：{len(unique_ids)}\n"
            f"✅ 成功删除：{deleted_count}个\n"
            f"❌ 失败数量：{len(failed_ids)}个"
        )
        if success_ids:
            result_msg += f"\n成功ID：{format_ids(success_ids)}"
        if failed_ids:
            result_msg += f"\n失败ID：{format_ids(failed_ids)}"
            
        await update.message.reply_text(result_msg)
    except Exception as e:
        await update.message.reply_text(f"❌ 删除操作异常：{str(e)}")

@restricted
async def handle_clear_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """发起清空确认"""
    await update.message.reply_text(
        "⚠️ 确认要清空数据库吗？此操作不可逆！\n"
        "回复「确认清空」继续，或任意内容取消"
    )
    return CONFIRM_CLEAR

@restricted
async def handle_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """执行清空操作"""
    text = update.message.text
    if text == '确认清空':
        if clear_database():
            await update.message.reply_text("✅ 数据库已清空")
        else:
            await update.message.reply_text("❌ 清空操作失败")
    else:
        await update.message.reply_text("❌ 操作已取消")
    return ConversationHandler.END

@restricted
async def cancel_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消清空操作"""
    await update.message.reply_text("❌ 已取消清空操作")
    return ConversationHandler.END

@restricted
async def handle_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """恢复STRM文件"""
    try:
        records = get_all_records()
        if not records:
            await update.message.reply_text("⚠️ 数据库中没有可恢复的记录")
            return
            
        await update.message.reply_text(f"🔄 正在恢复 {len(records)} 个文件...")
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
            except Exception as e:
                failed += 1
                
        await update.message.reply_text(
            f"✅ 恢复完成\n"
            f"成功: {success}个 | 失败: {failed}个"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 恢复失败：{str(e)}")

@restricted
async def handle_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """导入STRM文件"""
    try:
        start_time = datetime.now()
        report = import_strm_files()
        
        result_msg = (
            f"📦 导入完成！\n"
            f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒\n"
            f"🆕 新增: {report['imported']} | ⏩ 跳过: {report['skipped']}\n"
            f"⚠️ 无效: {report['invalid']} | ❌ 错误: {report['errors']}"
        )
        await update.message.reply_text(result_msg)
    except Exception as e:
        await update.message.reply_text(f"❌ 导入失败：{str(e)}")

# 新增 post_init 函数（放在调用之前）
async def post_init(application: Application):
    """设置机器人菜单命令"""
    commands = [
        BotCommand("delete", "删除指定ID的记录"),
        BotCommand("clear", "清空数据库"),
        BotCommand("restore", "恢复STRM文件"),
        BotCommand("import", "导入本地STRM文件")
    ]
    await application.bot.set_my_commands(commands)
    print(f"{Fore.CYAN}📱 Telegram命令菜单已加载")

# ========================= 初始化函数 =========================
def start_adapter():
    """Telegram Bot适配器入口（线程安全版）"""
    # 创建新事件循环（避免与主线程冲突）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 初始化核心组件
    init_db()
    os.makedirs(Config.OUTPUT_ROOT, exist_ok=True)
    
    # 构建应用
    request = HTTPXRequest(
        connection_pool_size=20,
        connect_timeout=180.0,
        read_timeout=180.0,
    )
    
    builder = (
        Application.builder()
        .token(Config.TG_TOKEN)
        .post_init(post_init)
        .get_updates_request(request)
    )
    
    app = builder.build()
    
    # 添加对话处理器
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
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("delete", handle_delete))
    app.add_handler(CommandHandler("restore", handle_restore))
    app.add_handler(CommandHandler("import", handle_import))
    app.add_handler(MessageHandler(
    filters.TEXT & 
    ~filters.COMMAND & 
    filters.Regex(r'https?://[^\s/]+/s/[a-zA-Z0-9\-_]+'),
    handle_message
))
    
    try:
        print(f"{Fore.GREEN}🤖 Telegram机器人已启动")
        app.run_polling()
    finally:
        loop.close()

if __name__ == "__main__":
    start_adapter()
