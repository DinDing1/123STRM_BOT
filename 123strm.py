#!/usr/bin/env python3
import os
import re
import aiofiles
import asyncio
import logging
from datetime import datetime
from colorama import init, Fore, Style
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from urllib.parse import unquote, urlparse
from typing import AsyncGenerator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from p123 import P123Client, check_response

# 初始化日志和颜色输出
init(autoreset=True)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

class Config:
    TG_TOKEN = os.getenv("TG_TOKEN", "")     
    BASE_URL = os.getenv("BASE_URL", "")     
    PROXY_URL = os.getenv("PROXY_URL", "")   
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt')
    PAN_PASSPORT = os.getenv("PAN_PASSPORT", "")
    PAN_PASSWORD = os.getenv("PAN_PASSWORD", "")
    REQUEST_INTERVAL = float(os.getenv("REQUEST_INTERVAL", "1.0"))
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))

class Async123Client:
    """异步123云盘客户端"""
    def __init__(self, passport: str, password: str):
        self.client = P123Client(passport, password)
    
    async def login(self):
        """异步登录"""
        await self.client.login(async_=True)
    
    @retry(
        stop=stop_after_attempt(Config.MAX_RETRIES),
        wait=wait_exponential(multiplier=1, max=10),
        retry=retry_if_exception_type(Exception)
    )
    async def safe_request(self, method, *args, **kwargs):
        """带重试机制的请求"""
        await asyncio.sleep(Config.REQUEST_INTERVAL)
        return await method(*args, **kwargs)
    
    async def async_share_iterdir(
        self,
        share_key: str,
        share_pwd: str,
        domain: str = "www.123pan.com",
        max_depth: int = -1
    ) -> AsyncGenerator[dict, None]:
        """异步遍历分享目录"""
        page = 1
        while True:
            try:
                resp = await self.safe_request(
                    self.client.share_fs_list,
                    {"ShareKey": share_key, "SharePwd": share_pwd, "Page": page},
                    base_url=f"https://{domain}",
                    async_=True
                )
                check_response(resp)
                
                # 验证响应数据结构
                if not isinstance(resp.get("data"), dict):
                    raise ValueError("Invalid data format in API response")
                if not isinstance(resp["data"].get("InfoList"), list):
                    raise ValueError("InfoList not found in response data")
                
                # 使用正确的分页字段
                next_page = resp["data"].get("Next", 0)
                
                for info in resp["data"]["InfoList"]:
                    if info.get("Type", 0):  # 跳过目录
                        continue
                        
                    # 标准化字段
                    info["url"] = info.get("DownloadURL", "")
                    info["relpath"] = info.get("FileName", "unknown")
                    yield info
                
                if next_page == 0:
                    break
                    
                page += 1
                
            except Exception as e:
                logging.error(f"分页请求异常 (第{page}页): {str(e)}")
                if "429" in str(e):
                    logging.warning("触发频率限制，等待10秒后重试...")
                    await asyncio.sleep(10)
                    continue
                raise

async def generate_strm_files(client: Async123Client, domain: str, share_key: str, share_pwd: str) -> dict:
    """生成STRM文件及字幕文件"""
    counts = {'video': 0, 'subtitle': 0, 'error': 0}
    base_url = Config.BASE_URL.rstrip('/')
    
    logging.info(f"开始处理 {domain} 的分享：{share_key}")

    try:
        async for info in client.async_share_iterdir(share_key, share_pwd, domain=domain):
            try:
                if not info["url"]:
                    logging.warning(f"跳过无下载链接的文件: {info['relpath']}")
                    continue

                ext = os.path.splitext(info["relpath"])[1].lower()
                if ext not in Config.VIDEO_EXTENSIONS + Config.SUBTITLE_EXTENSIONS:
                    continue

                output_path = os.path.join(Config.OUTPUT_ROOT, info["relpath"])
                os.makedirs(os.path.dirname(output_path), exist_ok=True)

                if ext in Config.VIDEO_EXTENSIONS:
                    strm_path = os.path.splitext(output_path)[0] + '.strm'
                    async with aiofiles.open(strm_path, 'w', encoding='utf-8') as f:
                        file_uri = unquote(info['url'].split('://', 1)[-1])
                        await f.write(f"{base_url}/{file_uri}")
                    counts['video'] += 1
                    logging.info(f"✅ 视频文件：{info['relpath']}")
                
                elif ext in Config.SUBTITLE_EXTENSIONS:
                    async with client.client.request(
                        info["url"],
                        method="GET",
                        headers={'User-Agent': 'Mozilla/5.0', 'Referer': f'https://{domain}/'},
                        async_=True
                    ) as resp:
                        resp.raise_for_status()
                        content = await resp.read()
                        async with aiofiles.open(output_path, 'wb') as f:
                            await f.write(content)
                    counts['subtitle'] += 1
                    logging.info(f"📝 字幕文件：{info['relpath']}")

            except Exception as e:
                counts['error'] += 1
                logging.error(f"处理异常 [{info.get('relpath', '未知文件')}]: {str(e)}")
    
    except Exception as e:
        counts['error'] += 1
        logging.error(f"遍历分享异常：{str(e)}")
    
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
        logging.info(f"Telegram代理已启用：{Config.PROXY_URL}")
    
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logging.info(f"机器人已启动 | 输出目录：{os.path.abspath(Config.OUTPUT_ROOT)}")
    app.run_polling()