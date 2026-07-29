#!/usr/bin/env bash
# تحميل وتثبيت ffmpeg بنسخة ثابتة
wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar -xf ffmpeg-release-amd64-static.tar.xz
mkdir -p /opt/render/project/bin
cp ffmpeg-*-amd64-static/ffmpeg /opt/render/project/bin/
cp ffmpeg-*-amd64-static/ffprobe /opt/render/project/bin/
# تثبيت متطلبات البايثون
pip install -r requirements.txt
