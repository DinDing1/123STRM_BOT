#!/usr/bin/env python3
import os
import re
import aiofiles
import asyncio
from datetime import datetime
from colorama import init, Fore, Style
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from urllib.parse import unquote, urlparse
from typing import AsyncGenerator

# 初始化colorama
init(autoreset=True)

class Config:
    TG_TOKEN = os.getenv("TG_TOKEN", "")     
    BASE_URL = os.getenv("BASE_URL", "")     
    PROXY_URL = os.getenv("PROXY_URL", "")   
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt')
    PAN_PASSPORT = os.getenv("PAN_PASSPORT", "")
    PAN_PASSWORD = os.getenv("PAN_PASSWORD", "")

class Async123Client:
    """异步123云盘客户端"""
    def __init__(self, passport: str, password: str):
        from p123 import P123Client
        self.client = P123Client(passport, password)
    
    async def login(self):
        """异步登录"""
        await self.client.login(async_=True)
    
    async def async_share_iterdir(
        self,
        share_key: str,
        share_pwd: str,
        domain: str = "www.123pan.com",
        max_depth: int = -1
    ) -> AsyncGenerator[dict, None]:
        """异步遍历分享目录"""
        from p123.tool import share_iterdir
        
        gen = share_iterdir(
            share_key,
            share_pwd,
            parent_id=0,
            max_depth=max_depth,
            predicate=lambda x: not x["is_dir"],
            async_=True,
            client=self.client,
            base_url=f"https://{domain}"
        )
        
        async for info in gen:
            # 标准化字段
            info["url"] = info.get("uri", "").split("?")[0]
            yield info

async def generate_strm_files(client: Async123Client, domain: str, share_key: str, share_pwd: str) -> dict:
    """生成STRM文件及字幕文件"""
    counts = {'video': 0, 'subtitle': 0, 'error': 0}
    base_url = Config.BASE_URL.rstrip('/')
    
    print(f"{Fore.YELLOW}🚀 开始处理 {domain} 的分享：{share_key}")

    try:
        async for info in client.async_share_iterdir(share_key, share_pwd, domain=domain):
            try:
                raw_uri = unquote(info["url"].split("://", 1)[-1])
                relpath = info["relpath"]
                ext = os.path.splitext(relpath)[1].lower()
                
                if ext not in Config.VIDEO_EXTENSIONS + Config.SUBTITLE_EXTENSIONS:
                    continue

                output_path = os.path.join(Config.OUTPUT_ROOT, relpath)
                os.makedirs(os.path.dirname(output_path), exist_ok=True)

                if ext in Config.VIDEO_EXTENSIONS:
                    strm_path = os.path.splitext(output_path)[0] + '.strm'
                    async with aiofiles.open(strm_path, 'w', encoding='utf-8') as f:
                        await f.write(f"{base_url}/{raw_uri}")
                    counts['video'] += 1
                    print(f"{Fore.GREEN}✅ 视频文件：{relpath}")
                
                elif ext in Config.SUBTITLE_EXTENSIONS:
                    download_url = f"https://{domain}/{raw_uri}"
                    async with self.client.client.request(
                        download_url,
                        method="GET",
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': f'https://{domain}/'},
                        async_=True
                    ) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
                        async with aiofiles.open(output_path, 'wb') as f:
                            await f.write(content)
                    counts['subtitle'] += 1
                    print(f"{Fore.BLUE}📝 字幕文件：{relpath}")

            except Exception as e:
                counts['error'] += 1
                print(f"{Fore.RED}❌ 处理异常：{relpath}\n{str(e)}")
    
    except Exception as e:
        counts['error'] += 1
        print(f"{Fore.RED}🔥 遍历分享异常：{str(e)}")
    
    return counts

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息"""
    msg = update.message.text
    pattern = r'(https?://[^\s/]+/s/)([\w-]+)[^\u4e00-\u9fa5]*(?:提取码|密码|code)[\s:：=]*(\w{4})'
    
    if not (match := re.search(pattern, msg, re.IGNORECASE)):
        await update.message.reply_text("❌ 链接格式错误！示例：\nhttps://xxx.xxx/s/xxxxxx 提取码：1234")
        return
    
    domain = urlparse(match.group(1)).netloc
    await update.message.reply_text(f"🔄 开始处理 {domain} 的分享")

    try:
        # 初始化客户端
        client = Async123Client(Config.PAN_PASSPORT, Config.PAN_PASSWORD)
        await client.login()
        
        start_time = datetime.now()
        report = await generate_strm_files(client, domain, match.group(2), match.group(3))
        
        result_msg = (
            f"✅ 处理完成！\n"
            f"⏱️ 耗时: {(datetime.now() - start_time).total_seconds():.1f}秒\n"
            f"🎬 视频: {report['video']} | 📝 字幕: {report['subtitle']}"
        )
        if report['error']:
            result_msg += f"\n❌ 错误: {report['error']}个"
        await update.message.reply_text(result_msg)
    
    except Exception as e:
        await update.message.reply_text(f"❌ 处理失败：{str(e)}")

if __name__ == "__main__":
    os.makedirs(Config.OUTPUT_ROOT, exist_ok=True)
    
    # 配置Telegram Bot
    builder = Application.builder().token(Config.TG_TOKEN)
    if Config.PROXY_URL:
        builder = builder.proxy(Config.PROXY_URL).get_updates_proxy(Config.PROXY_URL)
        print(f"{Fore.CYAN}🔗 Telegram代理已启用：{Config.PROXY_URL}")
    
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print(f"{Fore.GREEN}🤖 机器人已启动 | 输出目录：{os.path.abspath(Config.OUTPUT_ROOT)}")
    app.run_polling()