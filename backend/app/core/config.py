"""全局配置。所有数值从环境变量 / .env 读取，不写死在代码或前端。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # backend/
REPO_ROOT = BASE_DIR.parent

#: MVP 单用户工作区：所有项目归属同一个本地创作者。
#: 必须与 main._seed() 里创建的用户 id 一致，否则 User 1—N Project 关系会悬空。
LOCAL_USER_ID = "usr_local"
LOCAL_WORKSPACE_ID = "ws_local"


def _load_dotenv() -> None:
    for candidate in (REPO_ROOT / ".env", BASE_DIR / ".env"):
        if not candidate.exists():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


class Settings:
    """运行时配置。"""

    def __init__(self) -> None:
        self.app_name = "hoyo漫剧 API"
        self.version = "0.1.0-mvp"
        self.env = _env("APP_ENV", "dev")

        # --- 数据库 ---
        default_sqlite = (BASE_DIR / "data" / "hoyomanju.db").as_posix()
        self.database_url = _env("DATABASE_URL", f"sqlite:///{default_sqlite}")

        # --- 对象存储 ---
        self.storage_backend = _env("STORAGE_BACKEND", "local").lower()
        self.storage_dir = Path(_env("STORAGE_DIR", str(BASE_DIR / "data" / "storage")))
        self.s3_endpoint = _env("S3_ENDPOINT", "http://127.0.0.1:9000")
        self.s3_bucket = _env("S3_BUCKET", "hoyomanju")
        self.s3_access_key = _env("S3_ACCESS_KEY", "minioadmin")
        self.s3_secret_key = _env("S3_SECRET_KEY", "minioadmin")
        self.s3_region = _env("S3_REGION", "us-east-1")

        # --- 安全 ---
        self.secret_key = _env("SECRET_KEY", "dev-only-change-me-0123456789abcdef")
        self.signed_url_ttl_seconds = _env_int("SIGNED_URL_TTL_SECONDS", 1800)
        # §11.1-2：签名 URL 默认有效期不超过 30 分钟；调用方可传更短，但不能更长
        self.signed_url_max_ttl_seconds = _env_int("SIGNED_URL_MAX_TTL_SECONDS", 1800)

        # --- 队列 / worker ---
        self.redis_url = _env("REDIS_URL", "")
        self.worker_poll_interval = _env_float("WORKER_POLL_INTERVAL", 0.4)
        self.worker_lease_seconds = _env_int("WORKER_LEASE_SECONDS", 120)
        self.worker_concurrency = _env_int("WORKER_CONCURRENCY", 3)
        self.auto_retry_limit = _env_int("AUTO_RETRY_LIMIT", 1)
        # FR-072 自动重试的退避步长（秒）：第 n 次失败后退避 n * 该值
        self.retry_backoff_seconds = _env_int("RETRY_BACKOFF_SECONDS", 5)
        # 非终态任务超过该秒数仍未完成 → EXPIRED（0 表示不启用）
        self.job_expire_seconds = _env_int("JOB_EXPIRE_SECONDS", 86_400)
        self.job_timeout_seconds = _env_int("JOB_TIMEOUT_SECONDS", 600)
        # 「短时间内的重复点击」去重窗口：生成类接口的幂等键带一个时间桶，
        # 同一窗口内的重复请求命中同一个任务（不重复计费），窗口外视为一次新的生成。
        self.idempotency_window_seconds = _env_int("IDEMPOTENCY_WINDOW_SECONDS", 5)
        # 单机默认在 API 进程内嵌 worker；多实例部署时置 false 并单独起 worker 进程
        self.enable_embedded_worker = _env_bool("ENABLE_EMBEDDED_WORKER", True)

        # --- FR-061 转场 ---
        #: 未显式指定时长时的默认转场时长（毫秒）
        self.transition_default_ms = _env_int("TRANSITION_DEFAULT_MS", 300)
        #: 单个转场时长上限（毫秒）；实际还会受相邻两镜时长约束
        self.transition_max_ms = _env_int("TRANSITION_MAX_MS", 2000)

        # --- §11.3 上传限制：单文件大小 / 格式白名单 / 工作区总容量 ---
        self.upload_max_bytes = _env_int("UPLOAD_MAX_BYTES", 200 * 1024 * 1024)
        self.upload_workspace_quota_bytes = _env_int(
            "UPLOAD_WORKSPACE_QUOTA_BYTES", 5 * 1024 * 1024 * 1024
        )
        self.upload_allowed_mimes = [
            m.strip().lower()
            for m in _env(
                "UPLOAD_ALLOWED_MIMES",
                "image/png,image/jpeg,image/webp,image/gif,"
                "video/mp4,video/quicktime,"
                "audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/aac,audio/ogg,"
                "text/plain,text/markdown,application/pdf",
            ).split(",")
            if m.strip()
        ]

        # --- HTTP / 前端 ---
        self.cors_origins = [
            o.strip()
            for o in _env(
                "CORS_ORIGINS",
                "http://localhost:15731,http://127.0.0.1:15731,"
                "http://localhost:18437,http://127.0.0.1:18437",
            ).split(",")
            if o.strip()
        ]
        self.frontend_dist = _env("FRONTEND_DIST", "")

        # --- 媒体 ---
        self.ffmpeg_bin = _env("FFMPEG_BIN", "")
        self.preview_bitrate_kbps = _env_int("PREVIEW_BITRATE_KBPS", 800)
        self.export_video_bitrate_kbps = _env_int("EXPORT_VIDEO_BITRATE_KBPS", 2500)
        # 合成本身不调用外部供应商，但占用算力配额（§12-6：配额不足要阻断新任务）。
        # 计费口径：按输出时长（秒）× 单价（分/秒）。
        self.render_price_per_second_cents = _env_int("RENDER_PRICE_PER_SECOND_CENTS", 1)

        # --- 配额与限制（前端通过 /config/limits 读取） ---
        self.quota_limit_cents = _env_int("QUOTA_LIMIT_CENTS", 20000)
        self.max_concurrent_jobs = _env_int("MAX_CONCURRENT_JOBS", 3)
        self.candidates_per_shot_default = _env_int("CANDIDATES_PER_SHOT_DEFAULT", 2)
        self.candidates_per_shot_max = _env_int("CANDIDATES_PER_SHOT_MAX", 4)
        self.shot_duration_ms_min = _env_int("SHOT_DURATION_MS_MIN", 1000)
        self.shot_duration_ms_max = _env_int("SHOT_DURATION_MS_MAX", 15000)
        self.episode_duration_ms_max = _env_int("EPISODE_DURATION_MS_MAX", 600_000)
        self.story_text_max_chars = _env_int("STORY_TEXT_MAX_CHARS", 2000)
        self.min_shots = _env_int("MIN_SHOTS", 6)

        # --- 供应商（各模态一个主供应商） ---
        self.default_provider = _env("DEFAULT_PROVIDER", "mock")
        self.text_provider = _env("TEXT_PROVIDER", self.default_provider)
        self.image_provider = _env("IMAGE_PROVIDER", self.default_provider)
        self.video_provider = _env("VIDEO_PROVIDER", self.default_provider)
        self.speech_provider = _env("SPEECH_PROVIDER", self.default_provider)
        self.provider_timeout_seconds = _env_int("PROVIDER_TIMEOUT_SECONDS", 120)
        self.provider_max_retries = _env_int("PROVIDER_MAX_RETRIES", 1)
        self.ark_api_key = _env("ARK_API_KEY")
        self.ark_base_url = _env("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.ark_text_model = _env("ARK_TEXT_MODEL", "doubao-seed-1-6-250615")
        self.ark_image_model = _env("ARK_IMAGE_MODEL", "doubao-seedream-3-0-t2i-250415")
        self.ark_video_model = _env("ARK_VIDEO_MODEL", "doubao-seedance-1-0-pro-250528")
        self.openai_api_key = _env("OPENAI_API_KEY")
        self.openai_base_url = _env("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.openai_text_model = _env("OPENAI_TEXT_MODEL", "gpt-4o-mini")
        self.openai_image_model = _env("OPENAI_IMAGE_MODEL", "gpt-image-1")
        self.kling_api_key = _env("KLING_API_KEY")
        self.kling_base_url = _env("KLING_BASE_URL", "https://api-beijing.klingai.com")
        self.minimax_api_key = _env("MINIMAX_API_KEY")
        self.minimax_base_url = _env("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
        self.minimax_group_id = _env("MINIMAX_GROUP_ID")

        # --- DeepSeek（文本；OpenAI 兼容协议） ---
        self.deepseek_api_key = _env("DEEPSEEK_API_KEY")
        self.deepseek_base_url = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.deepseek_model = _env("DEEPSEEK_MODEL", "deepseek-chat")

        # --- toapis 聚合网关（图片 / 视频；统一走异步任务：提交拿 id，再轮询） ---
        self.gemini_image_api_key = _env("GEMINI_IMAGE_API_KEY")
        self.gemini_image_url = _env(
            "GEMINI_IMAGE_URL", "https://toapis.com/v1/images/generations"
        )
        # 注意：toapis 的模型 id 区分大小写，必须用小写的 gemini-nano-banana-2.1
        self.gemini_image_model = _env("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
        self.grok_video_api_key = _env("GROK_VIDEO_API_KEY")
        self.grok_video_url = _env("GROK_VIDEO_URL", "https://toapis.com/v1/videos/generations")
        self.grok_video_model = _env("GROK_VIDEO_MODEL", "grok-video-1.5")
        #: 异步任务轮询间隔与总超时（秒）
        self.task_poll_interval_seconds = _env_int("TASK_POLL_INTERVAL_SECONDS", 8)
        self.task_timeout_seconds = _env_int("TASK_TIMEOUT_SECONDS", 900)

        # --- 通义 MaaS TTS（同步返回音频 URL） ---
        self.tts_api_key = _env("TTS_API_KEY")
        self.tts_url = _env("TTS_URL")
        self.tts_api_model = _env("TTS_API_MODEL", "qwen-audio-3.1-tts-next")
        self.tts_voice = _env("TTS_VOICE", "Cherry")

        # --- 安全策略 ---
        self.safety_block_terms = [
            t.strip()
            for t in _env(
                "SAFETY_BLOCK_TERMS",
                "儿童色情,恐怖主义宣传,制毒教程,煽动暴力,极端仇恨",
            ).split(",")
            if t.strip()
        ]
        self.aigc_label_text = _env("AIGC_LABEL_TEXT", "本内容由 AI 生成")
        # FR-062 预览水印文案可配置
        self.preview_watermark_text = _env("PREVIEW_WATERMARK_TEXT", "预览版 · 未正式导出")

        # --- 门控默认策略 ---
        self.require_video_cost_confirmation = _env_bool("REQUIRE_VIDEO_COST_CONFIRMATION", True)
        self.enable_langgraph = _env_bool("ENABLE_LANGGRAPH", True)

        # --- 路径 ---
        self.data_dir = BASE_DIR / "data"
        self.checkpoint_db = Path(_env("CHECKPOINT_DB", str(self.data_dir / "checkpoints.db")))

        # --- §11.2-2 备份 ---
        self.backup_enabled = _env_bool("BACKUP_ENABLED", True)
        self.backup_dir = Path(_env("BACKUP_DIR", str(self.data_dir / "backups")))
        #: 每日备份保留份数（PRD 要求 7 点保留）
        self.backup_keep = _env_int("BACKUP_KEEP", 7)
        #: 自动备份间隔（小时）；0 表示只在启动时做一次
        self.backup_interval_hours = _env_int("BACKUP_INTERVAL_HOURS", 24)
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.storage_backend == "local":
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        if getattr(self, "backup_enabled", False):
            self.backup_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
