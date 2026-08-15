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
from flask import Flask, send_file
from threading import Thread

# لإسكات سجلات Flask العادية (200 OK) والتركيز على أخطاء البوت فقط
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ═══════════════════════════════════════════════════════════
# إعدادات البيئة والمخرجات
# ═══════════════════════════════════════════════════════════
os.environ['PYTHONUNBUFFERED'] = '1'
sys.stdout.reconfigure(line_buffering=True)
print = functools.partial(print, flush=True)

# ═══════════════════════════════════════════════════════════
# خادم Flask للبقاء حياً (Keep Alive)
# ═══════════════════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running..."

def run_server():
    flask_app.run(host='0.0.0.0', port=7860)

# Thread(target=run_server, daemon=True).start()

# إعداد Loop لـ Asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import yt_dlp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from pyrogram import Client

# ═══════════════════════════════════════════════════════════
# الإعدادات الأساسية
# ═══════════════════════════════════════════════════════════
API_ID                 = int(os.environ.get("API_ID", "0"))
API_HASH               = os.environ.get("API_HASH", "")
BOT_TOKEN              = os.environ.get("BOT_TOKEN", "")
CHAT_ID                = int(os.environ.get("CHAT_ID", "0"))
REFRESH_INTERVAL       = 120         
RECORD_DURATION_MINUTES = 15            # المدة المطلوبة 15 دقيقة
MAX_FILE_SIZE          = 1.9 * 1024 * 1024 * 1024
MAX_UPLOAD_RETRIES     = 3
STORAGE_LIMIT_GB       = 10.0
RECORDINGS_DIR         = "/tmp/recordings"

app = Client("Arkaiva_Session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
active_monitors = {}
monitors_lock   = threading.Lock()

# ═══════════════════════════════════════════════════════════
# دوال مساعدة (المساحة والمجلدات)
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
            print(f"🗑️  Auto-cleanup: Deleted oldest file {os.path.basename(fp)}")
    except: pass

# ═══════════════════════════════════════════════════════════
# الربط مع Google Sheets
# ═══════════════════════════════════════════════════════════
def get_from_sheets():
    try:
        scope  = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        # قراءة الكريدنشيالز من Secret بدل الملف
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not creds_json:
            print("⚠️  GOOGLE_CREDENTIALS_JSON secret not found!")
            return None
        
        creds_dict = json.loads(creds_json)
        creds  = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        
        client = gspread.authorize(creds)
        sheet  = client.open("MyTikTokList").sheet1
        usernames = sheet.col_values(1)[1:]
        return {name.strip() for name in usernames if name.strip()}
    except Exception as e:
        print(f"⚠️  Sheets error: {e}")
        return None

# ═══════════════════════════════════════════════════════════
# المعالجة والرفع (تعمل في الخلفية)
# ═══════════════════════════════════════════════════════════
def remux_to_mp4(ts_path: str, mp4_path: str) -> bool:
    # المحاولة الأولى: نسخ سريع مع إصلاح الأخطاء (بدون إعادة ترميز - سريع جدًا)
    cmd_copy = [
        'ffmpeg', '-y', 
        '-err_detect', 'ignore_err', # تجاهل الأخطاء البسيطة
        '-i', ts_path,
        '-c', 'copy', 
        '-bsf:a', 'aac_adtstoasc', # إصلاح مسار الصوت (مهم جدًا لملفات TS)
        '-movflags', '+faststart', 
        mp4_path
    ]
    
    # المحاولة الثانية: إعادة ترميز كاملة (إذا فشل النسخ السريع أو كان الملف تالفًا)
    cmd_reencode = [
        'ffmpeg', '-y', 
        '-err_detect', 'ignore_err',
        '-i', ts_path,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', # إعادة ترميز الصورة بسرعة فائقة
        '-c:a', 'aac', 
        '-movflags', '+faststart', 
        mp4_path
    ]

    try:
        # نجرب النسخ السريع أولاً
        result = subprocess.run(cmd_copy, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1200)
        
        if result.returncode != 0 or not os.path.exists(mp4_path) or os.path.getsize(mp4_path) < 1000:
            print(f"⚠️ Fast remux failed or file too small. Trying re-encoding...")
            # إذا فشل، أو كان الملف الناتج صغيرًا جدًا (دليل على تلف)، نقوم بإعادة الترميز
            if os.path.exists(mp4_path): os.remove(mp4_path)
            subprocess.run(cmd_reencode, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2400)
            
        return os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 1000
    except Exception as e:
        print(f"Error in remux_to_mp4: {e}")
        return False

async def upload_video_with_retry(path: str, caption: str):
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            print("Attempting to send to Telegram Bot...")
            await app.send_video(chat_id=CHAT_ID, video=path, caption=caption, supports_streaming=True)
            print("Sent successfully!")
            if os.path.exists(path): os.remove(path)
            return True
        except Exception as e:
            print(f"Failed to send to Telegram: {e}")
            print(f"⚠️  Upload failed (attempt {attempt}): {e}")
            await asyncio.sleep(20)
    if os.path.exists(path): os.remove(path)
    return False

def process_and_upload_task(raw_file, mp4_file, username, ts, user_dir):
    """هذه الدالة تعالج الملف القديم بينما البوت يسجل الملف الجديد"""
    try:
        if os.path.exists(raw_file) and os.path.getsize(raw_file) > 10000:
            print(f"🔄 Processing previous part for @{username}...")
            if remux_to_mp4(raw_file, mp4_file):
                os.remove(raw_file)
                file_size_mb = os.path.getsize(mp4_file) / (1024 * 1024)
                print(f"✅ Remux successful for @{username}. Size: {file_size_mb:.2f} MB. Starting upload...")
                
                # رفع الملف (أو تقسيمه إذا تجاوز الحد)
                if os.path.getsize(mp4_file) <= MAX_FILE_SIZE:
                    asyncio.run_coroutine_threadsafe(upload_video_with_retry(mp4_file, f"📹 @{username}"), loop)
                else:
                    os.remove(mp4_file)
            else:
                if os.path.exists(raw_file): os.remove(raw_file)
    except Exception as e:
        print(f"⚠️ Background task error @{username}: {e}")

# ═══════════════════════════════════════════════════════════
# التسجيل والمراقبة المتواصلة
# ═══════════════════════════════════════════════════════════
def record_stream(username: str, raw_file: str, stop_event: threading.Event):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    cmd = [
        'streamlink', 
        '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        '--hls-live-restart', 
        '--stream-segment-timeout', '30', 
        tiktok_url, '720p,best', '-o', raw_file
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start_t = time.time()
    duration = RECORD_DURATION_MINUTES * 60
    
    while not stop_event.is_set() and (time.time() - start_t) < duration:
        if process.poll() is not None: break
        time.sleep(5)
    
    if process.poll() is None:
        process.terminate()

def monitor(username: str, stop_event: threading.Event):
    tiktok_url = f"https://www.tiktok.com/@{username}/live"
    user_dir = get_user_dir(username)
    is_already_live = False 

    while not stop_event.is_set():
        # لا ننتظر إذا كنا في حالة بث مستمر لتقليل الفجوة
        if not is_already_live:
            time.sleep(random.randint(5, 45))
        
        try:
            # فحص البث باستخدام streamlink في وضع JSON
            is_live = False
            check_cmd = [
                'streamlink', 
                '--http-header', 'User-Agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                '--json', 
                tiktok_url
            ]
            
            result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=45)
            
            if result.returncode == 0 and result.stdout.strip():
                try:
                    output = json.loads(result.stdout)
                    if "streams" in output and len(output["streams"]) > 0:
                        is_live = True
                except json.JSONDecodeError:
                    pass

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
                # التسجيل (يحجز الكود هنا لمدة 15 دقيقة)
                record_stream(username, raw_file, stop_event)
                print("Recording finished successfully. File should be saved.")
            except Exception as e:
                print(f"Error during recording: {e}")

            # التحقق من وجود الملف وحجمه
            if os.path.exists(raw_file):
                print(f"File found! Size: {os.path.getsize(raw_file) / 1024 / 1024:.2f} MB")
            else:
                print("File was not created!")

            # فور انتهاء التسجيل، نطلق "خيط" للمعالجة والرفع ونعود فوراً لبداية الـ while
            threading.Thread(
                target=process_and_upload_task,
                args=(raw_file, mp4_file, username, ts, user_dir),
                daemon=True
            ).start()

            # انتظار ثانية واحدة فقط لضمان عدم تداخل التسميات
            time.sleep(1)

        except Exception as e:
            print(f"⚠️  Monitor error @{username}: {e}")
            time.sleep(10)

# ═══════════════════════════════════════════════════════════
# التشغيل والمزامنة
# ═══════════════════════════════════════════════════════════
def sync_monitors():
    while True:
        current_users = get_from_sheets()
        if current_users is not None:
            with monitors_lock:
                running = set(active_monitors.keys())
                for u in current_users - running:
                    stop_evt = threading.Event()
                    t = threading.Thread(target=monitor, args=(u, stop_evt), daemon=True)
                    t.start()
                    active_monitors[u] = {"stop": stop_evt}
                    print(f"👁️  Started monitoring: @{u}")
                for u in running - current_users:
                    active_monitors[u]["stop"].set()
                    del active_monitors[u]
                    print(f"🛑 Stopped monitoring: @{u}")
        time.sleep(REFRESH_INTERVAL)

async def main():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    await app.start()
    print("🤖 Bot is Online & Ready on GitHub Actions.")
    threading.Thread(target=sync_monitors, daemon=True).start()
    
    start_time = time.time()
    max_duration = 5.5 * 3600  # 5 ساعات ونصف كحد أقصى
    
    # فحص مستمر للوقت كل دقيقة بدلاً من النوم المتواصل
    while time.time() - start_time < max_duration:
        utc_now = datetime.datetime.now(datetime.UTC)
        baghdad_time = utc_now + datetime.timedelta(hours=3)
        
        # إذا دخلنا في وقت الاستراحة (بين 7:00 و 9:00 صباحاً بتوقيت بغداد)، نخرج من المراقبة
        if 7 <= baghdad_time.hour < 9:
            print("☕ Break time reached! Stopping early.")
            break
            
        await asyncio.sleep(60)  # يفحص الوقت كل 60 ثانية
    
    print("🛑 Stopping gracefully...")
    await app.stop()

    # بعد الإيقاف، نقرر: هل نمرر المهمة أم ننام؟
    utc_now = datetime.datetime.now(datetime.UTC)
    baghdad_time = utc_now + datetime.timedelta(hours=3)
    
    print(f"🕒 Current Baghdad Time: {baghdad_time.strftime('%H:%M:%S')}")

    if 7 <= baghdad_time.hour < 9:
        print("💤 Entering break mode. See you at 9:00 AM!")
        sys.exit(0)  # خروج طبيعي، لا تشغل السيرفر القادم
    else:
        print("🔄 Triggering the next relay run instantly...")
        sys.exit(42) # كود خروج سري يخبر سيرفرات جيت هاب بإطلاق السيرفر القادم فوراً

if __name__ == "__main__":
    try: 
        loop.run_until_complete(main())
    except KeyboardInterrupt: 
        pass
