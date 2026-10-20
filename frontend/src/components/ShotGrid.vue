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
  audioState: string
  selected: boolean
  active: boolean
}

const cards = computed<CardView[]>(() =>
  props.shots.map((shot) => {
    const candidates = shot.candidates ?? []
    const mediaId = shot.confirmed_media_id ?? shot.selected_media_id ?? candidates[0]?.media_id ?? null
    const candidate = candidates.find((c) => c.media_id === mediaId)
    return {
      shot,
      mediaId,
      url: candidate?.url ?? null,
      candidateCount: candidates.length,
      confirmed: Boolean(shot.confirmed_media_id),
      stale: Boolean(shot.stale),
      videoState: shot.video_skipped ? '已跳过' : (shot.video_status ?? ''),
      audioState: shot.audio_status ?? '',
      selected: props.selectedNos.includes(shot.shot_no),
      active: props.activeNo === shot.shot_no,
    }
  }),
)

function videoTone(state: string): string {
  const s = state.toUpperCase()
  if (s === 'SUCCEEDED' || s === 'READY' || s === 'CONFIRMED') return 'ok'
  if (s === 'RUNNING' || s === 'QUEUED' || s === 'PENDING') return 'info'
  if (s === 'FAILED') return 'danger'
  if (state === '已跳过') return 'warn'
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
          <span v-if="card.videoState" class="badge badge-dot" :class="`badge-${videoTone(card.videoState)}`">
            {{ card.videoState === '已跳过' ? '已跳过' : `视频 ${card.videoState}` }}
          </span>
          <span v-if="card.audioState" class="badge badge-dot badge-info">音频 {{ card.audioState }}</span>
          <span v-if="!card.videoState && !card.audioState" class="badge">未生成视频</span>
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
  box-shadow: 0 0 0 1px var(--line-accent), 0 10px 26px rgba(56, 189, 248, 0.12);
}
.shot-card.selected {
  border-color: rgba(167, 139, 250, 0.5);
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
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: rgba(6, 9, 14, 0.72);
  border: 1px solid var(--line);
  color: var(--text-0);
}
.shot-cand {
  position: absolute;
  left: 6px;
  bottom: 6px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: rgba(6, 9, 14, 0.72);
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
  font-size: 11px;
  font-weight: 700;
  background: var(--ok-soft);
  border: 1px solid rgba(52, 211, 153, 0.5);
  color: var(--ok);
}
.shot-stale {
  position: absolute;
  right: 6px;
  bottom: 6px;
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: var(--warn-soft);
  border: 1px solid rgba(251, 191, 36, 0.45);
  color: var(--warn);
}
.shot-info {
  padding: 8px 10px 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.shot-line1 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.shot-dur {
  font-size: 12.5px;
  font-weight: 600;
}
.shot-badges {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.shot-badges .badge {
  height: 18px;
  font-size: 10.5px;
  padding: 0 6px;
}
.shot-check {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 10px 9px;
  cursor: pointer;
}
</style>
