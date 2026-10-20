<script setup lang="ts">
/**
 * 候选图对比与确认：
 *  - 点选候选（POST /shots/{id}/select）
 *  - 确认画面（POST /shots/{id}/confirm）
 *  - 驳回问题候选（POST /shots/{id}/reject）
 *  - 恢复历史候选（POST /shots/{id}/restore-candidate）
 */
import { computed, ref, watch } from 'vue'
import { safetyLabel } from '@/api/shots'
import { relativeTime } from '@/utils/format'
import MediaThumb from './MediaThumb.vue'
import EmptyState from './EmptyState.vue'
import type { Shot, ShotCandidate } from '@/types'

const props = withDefaults(
  defineProps<{
    shot: Shot
    candidates: ShotCandidate[]
    busy?: boolean
    /** 当前已选中的候选（默认取 shot.selected_media_id）。 */
    selectedMediaId?: string | null
  }>(),
  { busy: false, selectedMediaId: null },
)

const emit = defineEmits<{
  select: [mediaId: string]
  confirm: [mediaId: string]
  reject: [mediaId: string, reason: string]
  restore: [mediaId: string]
}>()

const picked = ref<string | null>(null)
const compare = ref(false)
const rejectTarget = ref<string | null>(null)
const rejectReason = ref('')

watch(
  () => [props.shot.id, props.selectedMediaId],
  () => {
    picked.value = props.selectedMediaId ?? props.shot.selected_media_id ?? null
    compare.value = false
    rejectTarget.value = null
    rejectReason.value = ''
  },
  { immediate: true },
)

const confirmedId = computed(() => props.shot.confirmed_media_id ?? null)
const pickedCandidate = computed(
  () => props.candidates.find((c) => c.media_id === picked.value) ?? null,
)
const confirmedCandidate = computed(
  () => props.candidates.find((c) => c.media_id === confirmedId.value) ?? null,
)

function safetyTone(safety?: string): string {
  if (safety === 'PASS') return 'ok'
  if (safety === 'REVIEW') return 'warn'
  if (safety === 'BLOCK') return 'danger'
  return 'muted'
}

function pick(mediaId: string): void {
  picked.value = mediaId
  emit('select', mediaId)
}

function startReject(mediaId: string): void {
  rejectTarget.value = mediaId
  rejectReason.value = ''
}

function submitReject(): void {
  if (!rejectTarget.value || !rejectReason.value.trim()) return
  emit('reject', rejectTarget.value, rejectReason.value.trim())
  rejectTarget.value = null
  rejectReason.value = ''
}
</script>

<template>
  <div class="candidate-picker">
    <div class="cp-head">
      <div class="row">
        <span class="section-title">候选画面</span>
        <span class="mono tiny dim">{{ candidates.length }} 张</span>
      </div>
      <div class="row">
        <button
          v-if="pickedCandidate && confirmedCandidate && pickedCandidate.media_id !== confirmedCandidate.media_id"
          type="button"
          class="btn btn-sm btn-ghost"
          @click="compare = !compare"
        >
          {{ compare ? '收起对比' : '对比确认图' }}
        </button>
        <button
          v-if="pickedCandidate"
          type="button"
          class="btn btn-sm btn-primary"
          :disabled="busy || picked === confirmedId"
          @click="picked && emit('confirm', picked)"
        >
          {{ picked === confirmedId ? '已确认此图' : '确认为主图' }}
        </button>
      </div>
    </div>

    <EmptyState
      v-if="!candidates.length"
      compact
      icon="▣"
      title="还没有候选图"
      description="在上方选择镜头并生成分镜图，候选会出现在这里。"
    />

    <div v-else class="cp-grid">
      <figure
        v-for="cand in candidates"
        :key="cand.media_id"
        class="cp-item"
        :class="{ picked: picked === cand.media_id, confirmed: confirmedId === cand.media_id }"
        @click="pick(cand.media_id)"
      >
        <div class="cp-thumb">
          <MediaThumb :media-id="cand.media_id" :url="cand.url" portrait :alt="`候选 ${cand.version ?? ''}`" />
          <span v-if="confirmedId === cand.media_id" class="cp-flag ok">主图</span>
          <span v-else-if="picked === cand.media_id" class="cp-flag accent">已选</span>
          <span v-if="cand.status === 'REJECTED'" class="cp-flag danger">已驳回</span>
        </div>
        <figcaption class="cp-cap">
          <div class="row-between">
            <span class="mono tiny">v{{ cand.version ?? '—' }}</span>
            <span class="badge badge-dot" :class="`badge-${safetyTone(cand.safety)}`">
              {{ safetyLabel(cand.safety) }}
            </span>
          </div>
          <div class="tiny dim">{{ relativeTime(cand.created_at) }}</div>
          <p v-if="cand.rejected_reason" class="cp-reason tiny">{{ cand.rejected_reason }}</p>
          <div class="cp-actions" @click.stop>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="busy"
              @click="startReject(cand.media_id)"
            >
              驳回
            </button>
            <button
              v-if="cand.status === 'REJECTED'"
              type="button"
              class="btn btn-sm"
              :disabled="busy"
              @click="emit('restore', cand.media_id)"
            >
              恢复
            </button>
          </div>
        </figcaption>
      </figure>
    </div>

    <div v-if="rejectTarget" class="cp-reject">
      <span class="tiny dim">驳回原因（必填）</span>
      <textarea v-model="rejectReason" class="textarea" rows="2" placeholder="例如：人物脸崩、手指数量错误" />
      <div class="row">
        <button type="button" class="btn btn-sm btn-danger" :disabled="!rejectReason.trim()" @click="submitReject">
          提交驳回
        </button>
        <button type="button" class="btn btn-sm btn-ghost" @click="rejectTarget = null">取消</button>
      </div>
    </div>

    <div v-if="compare && pickedCandidate && confirmedCandidate" class="cp-compare">
      <div class="cp-compare-item">
        <div class="cp-compare-label tiny dim">当前主图</div>
        <MediaThumb :media-id="confirmedCandidate.media_id" :url="confirmedCandidate.url" portrait />
      </div>
      <div class="cp-compare-item">
        <div class="cp-compare-label tiny">待确认</div>
        <MediaThumb :media-id="pickedCandidate.media_id" :url="pickedCandidate.url" portrait />
      </div>
    </div>
  </div>
</template>

<style scoped>
.candidate-picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.cp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.cp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(142px, 1fr));
  gap: 12px;
}
.cp-item {
  margin: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
  overflow: hidden;
  cursor: pointer;
  transition:
    border-color var(--dur) var(--ease),
    transform var(--dur) var(--ease);
}
.cp-item:hover {
  border-color: var(--line-strong);
  transform: translateY(-2px);
}
.cp-item.picked {
  border-color: rgba(167, 139, 250, 0.6);
  box-shadow: 0 0 0 1px rgba(167, 139, 250, 0.35);
}
.cp-item.confirmed {
  border-color: rgba(52, 211, 153, 0.55);
}
.cp-thumb {
  position: relative;
}
.cp-flag {
  position: absolute;
  top: 6px;
  left: 6px;
  font-size: 10.5px;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--line);
  background: rgba(6, 9, 14, 0.76);
}
.cp-flag.ok {
  color: var(--ok);
  border-color: rgba(52, 211, 153, 0.5);
}
.cp-flag.accent {
  color: var(--accent-2);
  border-color: rgba(167, 139, 250, 0.5);
}
.cp-flag.danger {
  left: auto;
  right: 6px;
  color: var(--danger);
  border-color: rgba(248, 113, 113, 0.5);
}
.cp-cap {
  padding: 8px 10px 10px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.cp-reason {
  color: var(--warn);
}
.cp-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.cp-reject {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  border: 1px dashed var(--line-strong);
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.02);
}
.cp-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-1);
}
.cp-compare-label {
  margin-bottom: 6px;
}
</style>
