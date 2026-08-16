import os
import sys
import random
import functools
import asyncio
import time
import threading
import subprocess
import traceback
import json
import logging
import datetime
from pyrogram import Client
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask

# إعدادات البيئة
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
print = functools.partial(print, flush=True)

# ═══════════════════════════════════════════════════════════
# خادم Flask (لإرضاء Render وإبقاء البوت مستيقظاً)
# ═══════════════════════════════════════════════════════════
app_flask = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

@app_flask.route('/')
def home():
    return "✅ TikTok DVR Bot is Running 24/7 on Render!"

def run_flask():
    # Render تستخدم المنفذ 10000 افتراضياً
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
RECORD_DURATION_MINUTES = 15            
MAX_FILE_SIZE          = 1.9 * 1024 * 1024 * 1024
MAX_UPLOAD_RETRIES     = 3
STORAGE_LIMIT_GB       = 10.0
RECORDINGS_DIR         = "/tmp/recordings"

app = Client("Arkaiva_Session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_monitors = {}
monitors_lock   = threading.Lock()

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

def remux_to_mp4(ts_path: str, mp4_path: str) -> bool:
    print(f"🛠️ Fixing and remuxing {os.path.basename(ts_path)}")
    # استخدام أمر خفيف جداً على الرام يصلح التجمّد فقط
    cmd_fast = [
        'ffmpeg', '-y', '-err_detect', 'ignore_err',
        '-fflags', '+genpts+igndts', '-async', '1',
        '-i', ts_path, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', 
        '-movflags', '+faststart', mp4_path
    ]
    try:
        result = subprocess.run(cmd_fast, capture_output=True, timeout=1200)
        if result.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1024 * 500:
            print(f"✅ Remux successful.")
            return True
        return False
    except Exception as e:
        print(f"❌ Error in remux_to_mp4: {e}")
        return False

async def upload_video_with_retry(path: str, caption: str):
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            print("Attempting to send to Telegram...")
            await app.send_video(chat_id=CHAT_ID, video=path, caption=caption, supports_streaming=True)
            print("Sent successfully!")
            if os.path.exists(path): os.remove(path)
            return True
        except Exception as e:
            print(f"⚠️ Upload failed (attempt {attempt}): {e}")
            await asyncio.sleep(20)
    if os.path.exists(path): os.remove(path)
    return False

def process_and_upload_task(raw_file, mp4_file, username, ts, user_dir, loop):
    try:
        if os.path.exists(raw_file) and os.path.getsize(raw_file) > 10000:
            print(f"🔄 Processing previous part for @{username}...")
            # إذا فشل التحويل إلى mp4، نرفع الملف الخام .ts مباشرة لتجنب ضياع البث
            final_file = mp4_file if remux_to_mp4(raw_file, mp4_file) else raw_file
            
            if final_file == mp4_file and os.path.exists(raw_file):
                os.remove(raw_file)
                
            if os.path.getsize(final_file) <= MAX_FILE_SIZE:
                asyncio.run_coroutine_threadsafe(upload_video_with_retry(final_file, f"📹 @{username}"), loop)
            else:
                os.remove(final_file)
    except Exception as e:
        print(f"⚠️ Background task error @{username}: {e}")

def record_stream(username: str, raw_file: str, stop_event: threading.Event):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    cmd = [
        'streamlink', 
        '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        '--hls-live-restart', '--stream-segment-timeout', '30',
        '--hls-live-edge', '3', '--retry-streams', '1', '--retry-max', '3',
        tiktok_url, '720p,720p60,best', '-o', raw_file
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start_t = time.time()
    duration = RECORD_DURATION_MINUTES * 60
    while not stop_event.is_set() and (time.time() - start_t) < duration:
        if process.poll() is not None: break
        time.sleep(5)
    if process.poll() is None:
        process.terminate()

def monitor(username: str, stop_event: threading.Event, loop):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    user_dir = get_user_dir(username)
    is_already_live = False 

    while not stop_event.is_set():
        if not is_already_live:
            time.sleep(random.randint(5, 45))
        try:
            is_live = False
            check_cmd = [
                'streamlink', 
                '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                '--json', tiktok_url
            ]
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=45)
            if result.returncode == 0 and result.stdout.strip():
                try:
                    output = json.loads(result.stdout)
                    if "streams" in output and len(output["streams"]) > 0:
                        is_live = True
                except json.JSONDecodeError: pass

            if not is_live:
                is_already_live = False
                time.sleep(60)
                continue

            print(f"🔴 Live now: @{username}. Starting {RECORD_DURATION_MINUTES}-min segment...")
            is_already_live = True
            if not is_storage_safe(): emergency_cleanup()

            ts = int(time.time())
            raw_file = os.path.join(user_dir, f"{username}_{ts}_raw.ts")
            mp4_file = os.path.join(user_dir, f"{username}_{ts}.mp4")

            try:
                record_stream(username, raw_file, stop_event)
            except: pass

            threading.Thread(
                target=process_and_upload_task,
                args=(raw_file, mp4_file, username, ts, user_dir, loop),
                daemon=True
            ).start()
            time.sleep(1)

        except: time.sleep(10)

def sync_monitors(loop):
    while True:
        current_users = get_from_sheets()
        if current_users is not None:
            with monitors_lock:
                running = set(active_monitors.keys())
                for u in current_users - running:
                    stop_evt = threading.Event()
                    t = threading.Thread(target=monitor, args=(u, stop_evt, loop), daemon=True)
                    t.start()
                    active_monitors[u] = {"stop": stop_evt}
                    print(f"👁️ Started monitoring: @{u}")
                for u in running - current_users:
                    active_monitors[u]["stop"].set()
                    del active_monitors[u]
                    print(f"🛑 Stopped monitoring: @{u}")
        time.sleep(REFRESH_INTERVAL)

async def main():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    await app.start()
    print("🤖 Bot is Online & Ready on Render.")
    
    loop = asyncio.get_running_loop()
    threading.Thread(target=sync_monitors, args=(loop,), daemon=True).start()
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    # تشغيل خادم Flask لإرضاء Render
    threading.Thread(target=run_flask, daemon=True).start()
    
    # تشغيل البوت الأساسي
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt: pass
