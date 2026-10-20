# hoyo漫剧 后端镜像（FastAPI + FFmpeg）
# 构建：docker build -f backend/Dockerfile -t hoyomanju-api .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# FFmpeg：字幕烧录、转码、音频混流全部依赖它
# 中文字体：drawtext 渲染中文字幕必须要有，否则预览视频会缺字
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      fonts-noto-cjk \
      fonts-wqy-zenhei \
      curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/ ./

# 数据目录（SQLite / 本地对象存储 / checkpoint）
RUN mkdir -p /data/storage

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

# 首次启动自动建表；已有数据库时 create_all 不会破坏结构
CMD ["sh", "-c", "python -m alembic upgrade head || python -c \"from app.db.session import init_db; init_db()\"; exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
