<script setup lang="ts">
/**
 * 分镜缩略图网格：9:16 竖屏 + 候选图状态。
 * 支持多选（批量生成/批量确认）与单镜进入详情。
 */
import { computed } from 'vue'
import { formatDuration } from '@/utils/format'
import MediaThumb from './MediaThumb.vue'
import EmptyState from './EmptyState.vue'
import type { Shot } from '@/types'

const props = withDefaults(
  defineProps<{
    shots: Shot[]
    /** 已勾选的镜号。 */
    selectedNos?: number[]
    /** 当前聚焦的镜号。 */
    activeNo?: number | null
    /** 是否显示勾选框。 */
    selectable?: boolean
    /** 是否可点击进入详情。 */
    clickable?: boolean
  }>(),
  { selectedNos: () => [], activeNo: null, selectable: false, clickable: true },
)

const emit = defineEmits<{ toggle: [shotNo: number]; open: [shot: Shot] }>()

interface CardView {
  shot: Shot
  mediaId: string | null
  url: string | null
  candidateCount: number
  confirmed: boolean
  stale: boolean
  videoState: string
  videoLabel: string
  videoTone: string
  audioLabel: string
  audioState: string
  selected: boolean
  active: boolean
}

const cards = computed<CardView[]>(() =>
  props.shots.map((shot) => {
    const candidates = shot.candidates ?? []
    // `media_id` 由 api 层归一（后端主键叫 id）；这里再用 id 兜一次，
    // 避免绕过 fetchShots 直接把原始行塞进组件时又变成「暂无素材」。
    const firstId = candidates[0]?.media_id || candidates[0]?.id || null
    const mediaId = shot.confirmed_media_id ?? shot.selected_media_id ?? firstId
    const candidate = candidates.find((c) => (c.media_id || c.id) === mediaId)
    return {
      shot,
      mediaId,
      url: candidate?.url ?? null,
      candidateCount: candidates.length,
      confirmed: Boolean(shot.confirmed_media_id),
      stale: Boolean(shot.stale),
      videoState: shot.video_skipped ? '已跳过' : (shot.video_status ?? ''),
      videoLabel: videoLabel(shot.video_skipped ? 'SKIPPED' : (shot.video_status ?? '')),
      videoTone: videoTone(shot.video_skipped ? 'SKIPPED' : (shot.video_status ?? '')),
      audioLabel: audioLabel(shot.audio_status ?? ''),
      audioState: shot.audio_status ?? '',
      selected: props.selectedNos.includes(shot.shot_no),
      active: props.activeNo === shot.shot_no,
    }
  }),
)

/* 状态一律说人话：后端返回的是 VIDEO_STATUS 这类枚举，直接铺在卡片上没人看得懂 */
const VIDEO_LABELS: Record<string, string> = {
  PENDING: '待生成',
  QUEUED: '排队中',
  RUNNING: '生成中',
  SUCCEEDED: '已生成',
  CANDIDATES_READY: '待确认',
  CONFIRMED: '已确认',
  FAILED: '失败',
  SKIPPED: '已跳过',
  STALE: '已过期',
}
const AUDIO_LABELS: Record<string, string> = {
  PENDING: '待配音',
  RUNNING: '配音中',
  READY: '已配音',
  NO_DIALOGUE: '无台词',
  SKIPPED: '已跳过',
  FAILED: '失败',
}

function videoLabel(state: string): string {
  if (!state) return ''
  return VIDEO_LABELS[state.toUpperCase()] ?? '待生成'
}

function audioLabel(state: string): string {
  if (!state) return ''
  return AUDIO_LABELS[state.toUpperCase()] ?? '待配音'
}

function videoTone(state: string): string {
  const s = state.toUpperCase()
  if (s === 'SUCCEEDED' || s === 'READY' || s === 'CONFIRMED') return 'ok'
  if (s === 'CANDIDATES_READY') return 'warn'
  if (s === 'RUNNING' || s === 'QUEUED' || s === 'PENDING') return 'info'
  if (s === 'FAILED') return 'danger'
  if (s === 'SKIPPED') return 'muted'
  return 'muted'
}
</script>

<template>
  <EmptyState
    v-if="!cards.length"
    icon="▦"
    title="还没有分镜"
    description="先在「故事与剧本」里生成剧本，分镜表会自动出现。"
  >
    <slot name="empty-action" />
  </EmptyState>

  <div v-else class="shot-grid">
    <article
      v-for="card in cards"
      :key="card.shot.id"
      class="shot-card"
      :class="{ active: card.active, selected: card.selected, confirmed: card.confirmed }"
    >
      <div class="shot-thumb" @click="clickable && emit('open', card.shot)">
        <MediaThumb :media-id="card.mediaId" :url="card.url" portrait alt="分镜缩略图" />
        <span class="shot-no mono">#{{ String(card.shot.shot_no).padStart(3, '0') }}</span>
        <span v-if="card.candidateCount" class="shot-cand mono tiny">
          候选 {{ card.candidateCount }}
        </span>
        <span v-if="card.confirmed" class="shot-confirmed" title="已确认画面">✓</span>
        <span v-if="card.stale" class="shot-stale" title="下游资产已过期">过期</span>
      </div>

      <div class="shot-info">
        <div class="shot-line1">
          <span class="mono shot-dur">{{ formatDuration(card.shot.duration_ms) }}</span>
          <span class="shot-comp dim tiny">{{ card.shot.composition || '—' }}</span>
        </div>
        <div class="shot-chars tiny dim ellipsis">
          {{ (card.shot.characters ?? []).join('、') || '无角色' }}
        </div>
        <div class="shot-badges">
          <span v-if="card.videoLabel" class="badge badge-dot" :class="`badge-${card.videoTone}`">
            视频 · {{ card.videoLabel }}
          </span>
          <span v-if="card.audioLabel" class="badge badge-dot badge-info">
            音频 · {{ card.audioLabel }}
          </span>
          <span v-if="!card.videoLabel && !card.audioLabel" class="badge badge-muted">尚未生成</span>
        </div>
      </div>

      <label v-if="selectable" class="shot-check" @click.stop>
        <input
          type="checkbox"
          :checked="card.selected"
          @change="emit('toggle', card.shot.shot_no)"
        />
        <span class="tiny dim">选择</span>
      </label>
    </article>
  </div>
</template>

<style scoped>
.shot-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(164px, 1fr));
  gap: var(--gap);
}
.shot-card {
  position: relative;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  overflow: hidden;
  transition:
    border-color var(--dur) var(--ease),
    transform var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease);
}
.shot-card:hover {
  border-color: var(--line-strong);
  transform: translateY(-2px);
}
.shot-card.active {
  border-color: var(--line-accent);
  box-shadow: 0 0 0 1px var(--line-accent), 0 10px 26px rgba(224, 138, 60, 0.12);
}
.shot-card.selected {
  border-color: rgba(201, 116, 58, 0.5);
}
.shot-card.confirmed .shot-thumb::after {
  content: '';
  position: absolute;
  inset: 0;
  border-bottom: 2px solid var(--ok);
  pointer-events: none;
}
.shot-thumb {
  position: relative;
  cursor: pointer;
  background: var(--bg-1);
}
.shot-no {
  position: absolute;
  left: 6px;
  top: 6px;
  font-size: var(--fs-11);
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: rgba(6, 5, 4, 0.72);
  border: 1px solid var(--line);
  color: var(--text-0);
}
.shot-cand {
  position: absolute;
  left: 6px;
  bottom: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: rgba(6, 5, 4, 0.72);
  border: 1px solid var(--line);
  color: var(--text-1);
}
.shot-confirmed {
  position: absolute;
  right: 6px;
  top: 6px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: var(--fs-11);
  font-weight: 700;
  background: var(--ok-soft);
  border: 1px solid rgba(111, 191, 147, 0.5);
  color: var(--ok);
}
.shot-stale {
  position: absolute;
  right: 6px;
  bottom: 6px;
  font-size: var(--fs-11);
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: var(--warn-soft);
  border: 1px solid rgba(232, 203, 109, 0.45);
  color: var(--warn);
}
.shot-info {
  padding: var(--space-2) var(--space-3) 10px;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.shot-line1 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.shot-dur {
  font-size: var(--fs-12);
  font-weight: 600;
}
.shot-badges {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
}
.shot-badges .badge {
  height: 18px;
  font-size: var(--fs-11);
  padding: 0 6px;
}
.shot-check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 0 10px 9px;
  cursor: pointer;
}
</style>
