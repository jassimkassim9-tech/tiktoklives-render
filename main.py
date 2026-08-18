import os
import sys
import random
import functools
import asyncio
import time
import subprocess
import json
import logging
from threading import Thread
from pyrogram import Client
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask

# إعدادات البيئة
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
print = functools.partial(print, flush=True)

# ═══════════════════════════════════════════════════════════
# خادم Flask (لإرضاء Render) - يعمل في Thread مستقل
# ═══════════════════════════════════════════════════════════
app_flask = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app_flask.route('/')
def home():
    return "✅ TikTok DVR Bot is Running 24/7 on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host='0.0.0.0', port=port)

# ═══════════════════════════════════════════════════════════
# الإعدادات الأساسية
# ═══════════════════════════════════════════════════════════
API_ID                 = int(os.environ.get("API_ID", "0"))
API_HASH               = os.environ.get("API_HASH", "")
BOT_TOKEN              = os.environ.get("BOT_TOKEN", "")
CHAT_ID                = int(os.environ.get("CHAT_ID", "0"))
REFRESH_INTERVAL       = 120         
RECORD_DURATION_MINUTES = 20            
MAX_FILE_SIZE          = 1.9 * 1024 * 1024 * 1024 # 1.9 GB بفضل Pyrogram
MAX_UPLOAD_RETRIES     = 3
STORAGE_LIMIT_GB       = 10.0
RECORDINGS_DIR         = "/tmp/recordings"

# المتغيرات العامة
app = None 
active_monitors = {}
upload_queue = None

# ═══════════════════════════════════════════════════════════
# دوال المساعدة
# ═══════════════════════════════════════════════════════════
def get_user_dir(username: str) -> str:
    user_dir = os.path.join(RECORDINGS_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def get_used_storage_gb() -> float:
    try:
        if not os.path.exists(RECORDINGS_DIR): return 0.0
        total = sum(os.path.getsize(os.path.join(dp, f)) for dp, dn, filenames in os.walk(RECORDINGS_DIR) for f in filenames)
        return total / (1024 ** 3)
    except: return 0.0

def is_storage_safe() -> bool:
    return get_used_storage_gb() < STORAGE_LIMIT_GB

def emergency_cleanup():
    try:
        all_files = []
        for dirpath, _, filenames in os.walk(RECORDINGS_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                all_files.append((os.path.getmtime(fp), fp))
        all_files.sort()
        for _, fp in all_files[:5]:
            os.remove(fp)
            print(f"🗑️ Auto-cleanup: Deleted oldest file {os.path.basename(fp)}")
    except: pass

def get_from_sheets():
    try:
        scope  = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            print("⚠️ GOOGLE_CREDENTIALS_JSON secret not found!")
            return None
        creds_dict = json.loads(creds_json)
        creds  = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet  = client.open("MyTikTokList").sheet1
        usernames = sheet.col_values(1)[1:]
        return {name.strip() for name in usernames if name.strip()}
    except Exception as e:
        print(f"⚠️ Sheets error: {e}")
        return None

async def remux_to_mp4(ts_path: str, mp4_path: str) -> bool:
    print(f"🛠️ Fixing and remuxing {os.path.basename(ts_path)}")
    cmd_fast = [
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-fflags', '+genpts',
        '-i', ts_path, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', 
        mp4_path
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_fast, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        
        await asyncio.wait_for(process.communicate(), timeout=300)
        
        # قللنا حجم الفحص إلى 10 كيلوبايت، حتى لو كان المقطع قصيراً جداً سيعتبر ناجحاً
        if process.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1024 * 10:
            print(f"✅ Remux successful.")
            return True
            
        print(f"⚠️ Remux failed or file too small. Code: {process.returncode}")
        return False
    except asyncio.TimeoutError:
        print(f"❌ Timeout! ffmpeg got stuck on {os.path.basename(ts_path)}")
        if process: process.kill()
        return False
    except Exception as e:
        print(f"❌ Error in remux_to_mp4: {e}")
        return False

# ═══════════════════════════════════════════════════════════
# نظام الرفع (عامل التوصيل)
# ═══════════════════════════════════════════════════════════
async def upload_video_with_retry(path: str, caption: str):
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            print(f"🚀 Attempting to send to Telegram (Attempt {attempt})...")
            await app.send_video(chat_id=CHAT_ID, video=path, caption=caption, supports_streaming=True)
            print(f"✅ Sent successfully: {caption}")
            if os.path.exists(path): os.remove(path)
            return True
        except Exception as e:
            print(f"⚠️ Upload failed (attempt {attempt}): {e}")
        await asyncio.sleep(20)
        
    if os.path.exists(path): os.remove(path)
    return False

async def upload_worker():
    while True:
        try:
            path, caption = await upload_queue.get()
            print(f"📦 Upload worker picked up: {os.path.basename(path)}")
            await upload_video_with_retry(path, caption)
            upload_queue.task_done()
        except Exception as e:
            print(f"⚠️ Upload worker error: {e}")
            await asyncio.sleep(5)

# ═══════════════════════════════════════════════════════════
# المراقبة والتسجيل
# ═══════════════════════════════════════════════════════════
async def record_stream_async(username: str, raw_file: str, stop_event: asyncio.Event):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    cmd = [
        'streamlink', 
        '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        '--hls-live-restart', '--stream-segment-timeout', '30',
        '--hls-live-edge', '3', '--retry-streams', '1', '--retry-max', '3',
        tiktok_url, '720p,720p60,best', '-o', raw_file
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    
    start_t = time.time()
    duration = RECORD_DURATION_MINUTES * 60
    
    while not stop_event.is_set() and (time.time() - start_t) < duration:
        if process.returncode is not None: break
        await asyncio.sleep(5)
        
    if process.returncode is None:
        process.terminate()
        try:
            # ننتظر 5 ثوانٍ ليتوقف بهدوء
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            # رصاصة الرحمة: إذا عاند ولم يتوقف، نقتله فوراً لننتقل للرفع
            process.kill()
            await process.wait()

async def monitor_async(username: str, stop_event: asyncio.Event):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    user_dir = get_user_dir(username)
    is_already_live = False 

    while not stop_event.is_set():
        if not is_already_live:
            await asyncio.sleep(random.randint(5, 45))
        try:
            is_live = False
            check_cmd = [
                'streamlink', 
                '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                '--json', tiktok_url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *check_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
            
            if process.returncode == 0 and stdout:
                try:
                    output = json.loads(stdout.decode())
                    if "streams" in output and len(output["streams"]) > 0:
                        is_live = True
                except json.JSONDecodeError: pass

            if not is_live:
                is_already_live = False
                await asyncio.sleep(60)
                continue

            print(f"🔴 Live now: @{username}. Starting {RECORD_DURATION_MINUTES}-min segment...")
            is_already_live = True
            if not is_storage_safe(): emergency_cleanup()

            ts = int(time.time())
            raw_file = os.path.join(user_dir, f"{username}_{ts}_raw.ts")
            mp4_file = os.path.join(user_dir, f"{username}_{ts}.mp4")

            try:
                await record_stream_async(username, raw_file, stop_event)
            except: pass

            # معالجة الملف بعد التسجيل
            if os.path.exists(raw_file):
                # إذا كان البث قصيراً جداً أو معطوباً (أقل من 50 كيلو بايت)، تجاهله تماماً
                if os.path.getsize(raw_file) < 1024 * 50:
                    print(f"⚠️ Skipping upload for @{username}: Stream was too short or empty.")
                    os.remove(raw_file)
                else:
                    print(f"🔄 Processing previous part for @{username}...")
                    remux_success = await remux_to_mp4(raw_file, mp4_file)
                    
                    if remux_success:
                        if os.path.exists(raw_file): os.remove(raw_file)
                        caption = f"📹 @{username}"
                        await upload_queue.put((mp4_file, caption))
                        print(f"📥 Added {os.path.basename(mp4_file)} to upload queue.")
                    else:
                        print(f"⚠️ Skipping upload for @{username}: Remux failed.")
                        if os.path.exists(raw_file): os.remove(raw_file)
                        if os.path.exists(mp4_path): os.remove(mp4_path)

            await asyncio.sleep(1)

        except Exception as e:
            await asyncio.sleep(10)

async def sync_monitors_async():
    while True:
        current_users = get_from_sheets()
        if current_users is not None:
            running = set(active_monitors.keys())
            
            for u in current_users - running:
                stop_evt = asyncio.Event()
                task = asyncio.create_task(monitor_async(u, stop_evt))
                active_monitors[u] = {"stop": stop_evt, "task": task}
                print(f"👁️ Started monitoring: @{u}")
                
            for u in running - current_users:
                active_monitors[u]["stop"].set()
                active_monitors[u]["task"].cancel()
                del active_monitors[u]
                print(f"🛑 Stopped monitoring: @{u}")
                
        await asyncio.sleep(REFRESH_INTERVAL)

# ═══════════════════════════════════════════════════════════
# التشغيل الأساسي
# ═══════════════════════════════════════════════════════════
async def main():
    global app, upload_queue
    
    upload_queue = asyncio.Queue()
    
    # تهيئة وبدء عميل التلغرام
    app = Client("Arkaiva_Session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
    await app.start()
    print("🤖 Bot is Online & Ready on Render.")
    
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    
    asyncio.create_task(upload_worker())
    asyncio.create_task(sync_monitors_async())
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
