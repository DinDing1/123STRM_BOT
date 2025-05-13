from http import client
import os
import re
import asyncio
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from telethon import TelegramClient, events
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
from colorama import init, Fore, Style

init(autoreset=True)

# ========================= 权限装饰器 =========================
def restricted(func):
    """仅允许授权用户操作"""
    async def wrapped(event):
        user_id = event.sender_id
        if user_id not in Config.ADMINS:
            await event.reply("🚫 无操作权限")
            return
        return await func(event)
    return wrapped

# ========================= 消息处理器 =========================
async def handle_telegram_message(event):
    """处理123网盘链接"""
    msg = event.text
    
    # 匹配分享链接格式
    pattern = r'(https?://[^\s/]+/s/)([a-zA-Z0-9\-_]+)(?:[\s\S]*?(?:提取码|密码|code)[\s:：=]*(\w{4}))?'
    if not (match := re.search(pattern, msg, re.IGNORECASE)):
        await event.reply("❌ 链接格式错误，请发送标准的123云盘分享链接")
        return
    
    domain = urlparse(match.group(1)).netloc
    share_key = match.group(2)
    share_pwd = match.group(3) or ""
    
    await event.reply(f"🔄 正在生成 {share_key} 的STRM文件...")
    try:
        start_time = datetime.now()
        report = generate_strm_files(domain, share_key, share_pwd)
        id_ranges = format_duplicate_ids(report['skipped_ids'])
        
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
            
        await event.reply(result_msg)
    except Exception as e:
        print(f"{Fore.RED}❌ 处理失败：{str(e)}")
        await event.reply(f"❌ 处理失败：{str(e)}")

# ========================= 删除命令处理器 =========================
@restricted
async def handle_delete_command(event):
    """删除指定ID记录"""
    args = event.text.split()[1:]
    if not args:
        help_text = (
            "❌ 参数错误！使用方法：\n"
            "单个ID：/delete 123\n"
            "多个ID：/delete 123 456 789\n"
            "区间ID：/delete 100-200\n"
            "混合模式：/delete 100-200 250 300-350"
        )
        await event.reply(help_text)
        return

    raw_ids = []
    for arg in args:
        if '-' in arg:
            try:
                start, end = map(int, arg.split('-'))
                raw_ids.extend(range(min(start, end), max(start, end) + 1))
            except ValueError:
                await event.reply(f"❌ 无效区间格式：{arg}")
                return
        else:
            try:
                raw_ids.append(int(arg))
            except ValueError:
                await event.reply(f"❌ 无效ID格式：{arg}")
                return

    unique_ids = list(set(filter(lambda x: x > 0, raw_ids)))
    if not unique_ids:
        await event.reply("⚠️ 未提供有效ID")
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
            
        await event.reply(result_msg)
    except Exception as e:
        await event.reply(f"❌ 删除操作异常：{str(e)}")

# ========================= 清空数据库处理器 =========================
clear_states = {}

@restricted
async def handle_clear_command(event):
    """清空数据库流程"""
    await event.reply(
        "⚠️ 确认要清空数据库吗？此操作不可逆！\n"
        "回复「确认清空」继续，或任意内容取消"
    )
    clear_states[event.chat_id] = True
    await asyncio.sleep(60)  # 60秒超时
    if event.chat_id in clear_states:
        del clear_states[event.chat_id]
        await event.reply("❌ 操作超时已取消")

@restricted
async def handle_clear_confirm(event):
    """确认清空操作"""
    if event.chat_id not in clear_states:
        return
    
    if event.text == '确认清空':
        if clear_database():
            await event.reply("✅ 数据库已清空")
        else:
            await event.reply("❌ 清空操作失败")
    else:
        await event.reply("❌ 操作已取消")
    del clear_states[event.chat_id]

# ========================= 恢复命令处理器 =========================
@restricted
async def handle_restore_command(event):
    """恢复STRM文件"""
    try:
        records = get_all_records()
        if not records:
            await event.reply("⚠️ 数据库中没有可恢复的记录")
            return
            
        await event.reply(f"🔄 正在恢复 {len(records)} 个文件...")
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
                
        await event.reply(
            f"✅ 恢复完成\n"
            f"成功: {success}个 | 失败: {failed}个"
        )
    except Exception as e:
        await event.reply(f"❌ 恢复失败：{str(e)}")

# ========================= 导入命令处理器 =========================
@restricted
async def handle_import_command(event):
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
        await event.reply(result_msg)
    except Exception as e:
        await event.reply(f"❌ 导入失败：{str(e)}")

# ========================= 初始化函数 =========================
def start_adapter():
    """Telethon适配器入口（线程安全版）"""
    # 创建新事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 初始化客户端
    client = TelegramClient(
        Config.TG_SESSION,
        int(Config.TG_API_ID),
        Config.TG_API_HASH,
        loop=loop
    )
    
    # 命令处理器注册
    @client.on(events.NewMessage(pattern=r'^/delete\b'))
    async def delete_handler(event):
        await handle_delete_command(event)
    
    @client.on(events.NewMessage(pattern=r'^/clear\b'))
    async def clear_handler(event):
        await handle_clear_command(event)
    
    @client.on(events.NewMessage(pattern=r'^/restore\b'))
    async def restore_handler(event):
        await handle_restore_command(event)
    
    @client.on(events.NewMessage(pattern=r'^/import\b'))
    async def import_handler(event):
        await handle_import_command(event)
    
    # 清空确认处理器
    @client.on(events.NewMessage(func=lambda e: e.chat_id in clear_states))
    async def clear_confirmation(event):
        await handle_clear_confirm(event)
    
    # 通用消息处理器
    # 精确匹配消息格式
    @client.on(events.NewMessage(
        func=lambda e: not e.text.startswith('/') and 
        re.search(r'https?://[^\s/]+/s/[a-zA-Z0-9\-_]+', e.text or '')
    ))
    async def message_handler(event):
        await handle_telegram_message(event)
        
      
    # 运行客户端
    with client:
        print(f"{Fore.GREEN}🤖 Telegram客户端已启动 (User Mode)")
        client.run_until_disconnected()
