import os
import re
import requests
from p123.tool import share_iterdir
from datetime import datetime
from colorama import init, Fore, Style
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from urllib.parse import unquote, urlparse

# 初始化colorama
init(autoreset=True)

class Config:
    TG_TOKEN = os.getenv("TG_TOKEN", "")     
    BASE_URL = os.getenv("BASE_URL", "")     
    PROXY_URL = os.getenv("PROXY_URL", "")   
    OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "./strm_output")
    VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.iso', '.rmvb', '.m2ts')
    SUBTITLE_EXTENSIONS = ('.srt', '.ass', '.sub', '.ssa', '.vtt')
    MAX_DEPTH = -1

def generate_strm_files(domain: str, share_key: str, share_pwd: str):
    """生成STRM文件及字幕文件"""
    base_url = Config.BASE_URL.rstrip('/')
    counts = {'video': 0, 'subtitle': 0, 'error': 0}
    
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
                strm_path = os.path.splitext(output_path)[0] + '.strm'
                with open(strm_path, 'w', encoding='utf-8') as f:
                    f.write(f"{base_url}/{raw_uri}")
                counts['video'] += 1
                print(f"{Fore.GREEN}✅ 视频文件：{relpath}")
            
            elif ext in Config.SUBTITLE_EXTENSIONS:
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
            print(f"{Fore.RED}❌ 处理异常：{relpath}\n{str(e)}")
    
    return counts

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理消息"""
    msg = update.message.text
    pattern = r'(https?://[^\s/]+/s/)([\w-]+)[^\u4e00-\u9fa5]*(?:提取码|密码|code)[\s:：=]*(\w{4})'
    
    if not (match := re.search(pattern, msg, re.IGNORECASE)):
        await update.message.reply_text("❌ 链接格式错误！示例：\nhttps://xxx.xxx/s/xxxxxx 提取码：1234")
        return
    
    domain = urlparse(match.group(1)).netloc
    share_key = match.group(2)
    await update.message.reply_text(f"🔄 开始处理 {share_key} 的分享")

    try:
        start_time = datetime.now()
        report = generate_strm_files(domain, match.group(2), match.group(3))
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
    
    # 修正后的代理配置
    builder = Application.builder().token(Config.TG_TOKEN)
    if Config.PROXY_URL:
        builder = (
            builder
            .proxy(Config.PROXY_URL)  # 正确参数传递方式
            .get_updates_proxy(Config.PROXY_URL)
        )
        print(f"{Fore.CYAN}🔗 Telegram代理已启用：{Config.PROXY_URL}")
    
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print(f"{Fore.GREEN}🤖 TG机器人已启动 | STRM输出目录：{os.path.abspath(Config.OUTPUT_ROOT)}")
    app.run_polling()
