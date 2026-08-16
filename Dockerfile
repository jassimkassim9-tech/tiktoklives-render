FROM python:3.12-slim

WORKDIR /app

# قمنا بإضافة gcc و build-essential هنا لتتمكن مكتبة TgCrypto من التثبيت بنجاح
RUN apt-get update && apt-get install -y ffmpeg gcc build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# تشغيل الكود
CMD ["python", "main.py"]
