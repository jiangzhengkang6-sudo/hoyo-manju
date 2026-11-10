"""SQLAlchemy 2.0 数据模型（对应需求文档 §7 数据模型）。"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.security import new_id


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """统一按 UTC 存取的时间戳。

    SQLite 不保存 tzinfo，直接读回的是 naive datetime，和 `utcnow()` 相减会抛
    `TypeError: can't subtract offset-naive and offset-aware datetimes`。
    这里在绑定与读回时统一补齐 UTC 时区，避免调用方到处做兼容。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


def _id(prefix: str):
    return mapped_column(String(48), primary_key=True, default=lambda: new_id(prefix))


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #

class EpisodeStage(str, enum.Enum):
    DRAFT = "DRAFT"
    SCRIPT_REVIEW = "SCRIPT_REVIEW"
    ASSET_REVIEW = "ASSET_REVIEW"
    SHOT_REVIEW = "SHOT_REVIEW"
    VIDEO_REVIEW = "VIDEO_REVIEW"
    AUDIO_REVIEW = "AUDIO_REVIEW"
    RENDERING = "RENDERING"
    PREVIEW_READY = "PREVIEW_READY"
    EXPORTED = "EXPORTED"
    # §10.1：任何阶段都允许进入 BLOCKED；解除阻塞后回到原阶段
    BLOCKED = "BLOCKED"
    # §10.1：上游版本变更后受影响资源被标记为 STALE，由用户决定是否重新生成
    STALE = "STALE"


class JobStatus(str, enum.Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.EXPIRED.value,
}


class AssetStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    REJECTED = "REJECTED"


class ApprovalDecision(str, enum.Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    SKIP = "SKIP"


class WorkflowStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


# --------------------------------------------------------------------------- #
# 账号 / 项目 / 剧集
# --------------------------------------------------------------------------- #

class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = _id("usr")
    name: Mapped[str] = mapped_column(String(120), default="本地创作者")
    workspace_id: Mapped[str] = mapped_column(String(48), default="ws_local")
    quota_limit_cents: Mapped[int] = mapped_column(Integer, default=0)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = _id("prj")
    user_id: Mapped[str] = mapped_column(String(48), index=True)
    name: Mapped[str] = mapped_column(String(200))
    genre: Mapped[str] = mapped_column(String(80), default="都市")
    aspect_ratio: Mapped[str] = mapped_column(String(16), default="9:16")
    default_language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    default_style_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: 统一语速：新建角色/从剧本同步角色时的初始 `voice_speed`（FR-051）
    default_voice_speed: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")  # ACTIVE / ARCHIVED

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_project_name_per_user"),)


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    id: Mapped[str] = _id("epi")
    project_id: Mapped[str] = mapped_column(String(48), ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    episode_no: Mapped[int] = mapped_column(Integer, default=1)
    target_duration_ms: Mapped[int] = mapped_column(Integer, default=60_000)
    stage: Mapped[str] = mapped_column(String(32), default=EpisodeStage.DRAFT.value)
    stage_before_block: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="ACTIVE")  # ACTIVE / ARCHIVED
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(lazy="joined")


class StoryDocument(Base, TimestampMixin):
    __tablename__ = "story_documents"
    id: Mapped[str] = _id("std")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    input_type: Mapped[str] = mapped_column(String(16), default="idea")  # idea/text/manual
    source_text: Mapped[str] = mapped_column(Text, default="")
    story_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")  # DRAFT/CONFIRMED/STALE


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    id: Mapped[str] = _id("sht")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    shot_no: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, default=4000)
    shot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(32), default="DRAFT")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    selected_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    confirmed_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    image_status: Mapped[str] = mapped_column(String(24), default="PENDING")
    video_status: Mapped[str] = mapped_column(String(24), default="PENDING")
    video_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    audio_status: Mapped[str] = mapped_column(String(24), default="PENDING")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "shot_no", name="uq_shot_no_per_episode"),
        Index("ix_shots_episode_no", "episode_id", "shot_no"),
    )


class CharacterAsset(Base, TimestampMixin):
    """角色卡（spec_json + 主参考图）。

    `episode_id` 为空表示**项目级公共角色**（手工新增，所有剧集可见）；
    非空表示从该集剧本抓取、只属于那一集 —— 同一个项目下可能有多集互不相干的
    故事（异世界 / 刑侦），它们各自都有一个叫「陈默」的主角，不能合成一张卡。
    """

    __tablename__ = "character_assets"
    id: Mapped[str] = _id("chr")
    project_id: Mapped[str] = mapped_column(String(48), ForeignKey("projects.id"), index=True)
    episode_id: Mapped[str | None] = mapped_column(
        String(48), ForeignKey("episodes.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    primary_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # FR-051：语速（倍率）。1.0 为原速，允许 0.5~2.0；随台词音频一起留档。
    voice_speed: Mapped[float] = mapped_column(Float, default=1.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default=AssetStatus.DRAFT.value)
    is_main: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("episode_id", "name", name="uq_character_name_per_episode"),
    )


class StyleAsset(Base, TimestampMixin):
    __tablename__ = "style_assets"
    id: Mapped[str] = _id("sty")
    project_id: Mapped[str] = mapped_column(String(48), ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="默认画风")
    description: Mapped[str] = mapped_column(Text, default="")
    palette: Mapped[list[Any]] = mapped_column(JSON, default=list)
    camera_style: Mapped[str] = mapped_column(Text, default="")
    lighting_style: Mapped[str] = mapped_column(Text, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default=AssetStatus.CONFIRMED.value)


class ShotCharacterRef(Base, TimestampMixin):
    __tablename__ = "shot_character_refs"
    id: Mapped[str] = _id("scr")
    shot_id: Mapped[str] = mapped_column(String(48), ForeignKey("shots.id"), index=True)
    character_id: Mapped[str] = mapped_column(String(48), ForeignKey("character_assets.id"))
    character_version: Mapped[int] = mapped_column(Integer, default=1)
    media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)

    __table_args__ = (UniqueConstraint("shot_id", "character_id", name="uq_shot_character"),)


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"
    id: Mapped[str] = _id("med")
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    owner_type: Mapped[str] = mapped_column(String(32), default="episode")
    owner_id: Mapped[str] = mapped_column(String(48), default="", index=True)
    kind: Mapped[str] = mapped_column(String(24), default="image")  # image/video/audio/text
    role: Mapped[str] = mapped_column(String(32), default="")  # shot_image/video/tts/bgm/export...
    uri: Mapped[str] = mapped_column(String(512), default="")
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(96), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default=AssetStatus.CANDIDATE.value)
    aigc_labeled: Mapped[bool] = mapped_column(Boolean, default=True)
    safety_status: Mapped[str] = mapped_column(String(24), default="PENDING")
    safety_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rights_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    rights_note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(64), default="generated")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# 工作流 / 任务 / 成本
# --------------------------------------------------------------------------- #

class WorkflowRun(Base, TimestampMixin):
    __tablename__ = "workflow_runs"
    id: Mapped[str] = _id("wfr")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    graph_version: Mapped[str] = mapped_column(String(32), default="v1")
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    current_node: Mapped[str] = mapped_column(String(64), default="initialize")
    pending_gate: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=WorkflowStatus.RUNNING.value)
    engine: Mapped[str] = mapped_column(String(24), default="langgraph")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    id: Mapped[str] = _id("job")
    workflow_run_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.CREATED.value, index=True)
    stage: Mapped[str] = mapped_column(String(48), default="")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    #: 自动重试的下次可执行时间（FR-072：失败后可有限自动重试，退避后再入队）
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(48), default="")
    model: Mapped[str] = mapped_column(String(96), default="")
    cost_reserved_cents: Mapped[int] = mapped_column(Integer, default=0)
    cost_settled_cents: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parent_job_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    result_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )


class ProviderCall(Base, TimestampMixin):
    __tablename__ = "provider_calls"
    id: Mapped[str] = _id("pvc")
    job_id: Mapped[str] = mapped_column(String(48), ForeignKey("jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(48), default="")
    model: Mapped[str] = mapped_column(String(96), default="")
    kind: Mapped[str] = mapped_column(String(24), default="")
    request_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    provider_job_id: Mapped[str] = mapped_column(String(128), default="")
    price_version: Mapped[str] = mapped_column(String(32), default="v1")
    billing_unit: Mapped[str] = mapped_column(String(32), default="call")
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimated_cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="SUCCESS")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    id: Mapped[str] = _id("apv")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    stage: Mapped[str] = mapped_column(String(64), default="")
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(48), default="")
    decision: Mapped[str] = mapped_column(String(24), default=ApprovalDecision.APPROVE.value)
    actor_id: Mapped[str] = mapped_column(String(48), default="local-user")
    asset_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CostLedger(Base, TimestampMixin):
    __tablename__ = "cost_ledgers"
    id: Mapped[str] = _id("cst")
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), default="SETTLE")  # RESERVE/SETTLE/RELEASE
    stage: Mapped[str] = mapped_column(String(48), default="")
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    note: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id: Mapped[str] = _id("aud")
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), default="")
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(48), default="")
    actor_id: Mapped[str] = mapped_column(String(48), default="local-user")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventLog(Base, TimestampMixin):
    """SSE 事件持久化，用于断线补齐（last_event_id）。"""

    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EntityVersion(Base, TimestampMixin):
    """剧本 / 角色描述 / 提示词 / 时间线版本记录（FR-004）。"""

    __tablename__ = "entity_versions"
    id: Mapped[str] = _id("ver")
    episode_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(48), default="", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_digest: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")


class WorkflowNodeLog(Base, TimestampMixin):
    __tablename__ = "workflow_node_logs"
    id: Mapped[str] = _id("wnl")
    workflow_run_id: Mapped[str] = mapped_column(String(48), index=True)
    episode_id: Mapped[str] = mapped_column(String(48), index=True)
    node: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(24), default="SUCCESS")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# 音频 / 字幕 / 导出
# --------------------------------------------------------------------------- #

class Voice(Base, TimestampMixin):
    __tablename__ = "voices"
    id: Mapped[str] = _id("voc")
    project_id: Mapped[str] = mapped_column(String(48), ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(48), default="mock")
    provider_voice_id: Mapped[str] = mapped_column(String(96), default="")
    gender: Mapped[str] = mapped_column(String(16), default="female")
    preview_media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")


class AudioTrack(Base, TimestampMixin):
    __tablename__ = "audio_tracks"
    id: Mapped[str] = _id("aud")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    character_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="dialogue")  # dialogue/bgm/ambience/sfx
    media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    voice_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    emotion: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(48), default="")
    #: 该音轨对应「镜头台词数组」里的第几句（0 起）。
    #: 没有它就只能按 start_ms 位置硬猜——中间有一句失败/被跳过时，
    #: 后面的音轨会整体错位（状态表显示已生成，实际是上一句的音频）。
    line_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    volume_db: Mapped[float] = mapped_column(Float, default=0.0)
    fade_in_ms: Mapped[int] = mapped_column(Integer, default=0)
    fade_out_ms: Mapped[int] = mapped_column(Integer, default=0)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    stale_reason: Mapped[str] = mapped_column(Text, default="")
    rights_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    # §12-4 降级：TTS 失败时允许用户「上传音频」顶替（manual）或「跳过配音」（skipped）。
    # 两者都要能继续预览/导出，所以必须与 tts 区分开，否则无法判断某条是没生成还是被有意跳过。
    source: Mapped[str] = mapped_column(String(16), default="tts")  # tts/manual/skipped


class SubtitleCue(Base, TimestampMixin):
    __tablename__ = "subtitle_cues"
    id: Mapped[str] = _id("sub")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    index_no: Mapped[int] = mapped_column(Integer, default=1)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)
    end_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    speaker: Mapped[str] = mapped_column(String(120), default="")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class ExportRecord(Base, TimestampMixin):
    __tablename__ = "exports"
    id: Mapped[str] = _id("exp")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="preview")  # preview/export/package
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    preset: Mapped[str] = mapped_column(String(32), default="720p_9x16")
    job_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    media_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    watermarked: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #

class ProviderSetting(Base, TimestampMixin):
    __tablename__ = "provider_settings"
    id: Mapped[str] = _id("pvs")
    kind: Mapped[str] = mapped_column(String(24), unique=True)  # text/image/video/speech
    provider: Mapped[str] = mapped_column(String(48), default="mock")
    model: Mapped[str] = mapped_column(String(96), default="")
    base_url: Mapped[str] = mapped_column(String(255), default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    api_key_masked: Mapped[str] = mapped_column(String(64), default="")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    # FR-090：备用供应商。主供应商返回可重试失败（超时/限流/不可用）时自动切换。
    fallback_provider: Mapped[str] = mapped_column(String(48), default="")
    fallback_model: Mapped[str] = mapped_column(String(96), default="")
    fallback_base_url: Mapped[str] = mapped_column(String(255), default="")
    fallback_api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    fallback_api_key_masked: Mapped[str] = mapped_column(String(64), default="")


class PriceBook(Base, TimestampMixin):
    __tablename__ = "price_book"
    id: Mapped[str] = _id("prc")
    provider: Mapped[str] = mapped_column(String(48), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(96), default="")
    price_version: Mapped[str] = mapped_column(String(32), default="v1")
    billing_unit: Mapped[str] = mapped_column(String(32), default="call")
    unit_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    note: Mapped[str] = mapped_column(Text, default="")


class SafetyAppeal(Base, TimestampMixin):
    """§12-8 内容拦截申诉。

    「输出内容被拦截 → 禁止确认和导出，提供修改输入或申诉入口」。
    申诉是**人工复核通道**：系统本身不自动解禁，只有 `status=APPROVED` 的申诉
    才会让对应素材在导出闸门上放行（见 `safety_agent.export_gate`）。
    """

    __tablename__ = "safety_appeals"
    id: Mapped[str] = _id("apl")
    episode_id: Mapped[str] = mapped_column(String(48), ForeignKey("episodes.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    # asset：某个被拦截的素材；input：被拦截的剧本输入（此时没有素材）；episode：整集
    target_type: Mapped[str] = mapped_column(String(24), default="asset")
    target_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING/APPROVED/REJECTED
    resolution_note: Mapped[str] = mapped_column(Text, default="")
