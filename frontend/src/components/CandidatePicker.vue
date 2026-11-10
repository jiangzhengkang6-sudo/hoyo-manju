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
const showBroken = ref(false)

watch(
  () => [props.shot.id, props.selectedMediaId],
  () => {
    picked.value = props.selectedMediaId ?? props.shot.selected_media_id ?? null
    compare.value = false
    rejectTarget.value = null
    rejectReason.value = ''
    showBroken.value = false
  },
  { immediate: true },
)

/** 文件已失效的候选（历史同名覆盖的受害者）单独收起来，不参与选择。 */
const brokenCandidates = computed(() => props.candidates.filter((c) => c.broken))
const healthyCandidates = computed(() => props.candidates.filter((c) => !c.broken))
const usableCount = computed(() => healthyCandidates.value.filter((c) => c.usable !== false).length)

const confirmedId = computed(() => props.shot.confirmed_media_id ?? null)
const pickedCandidate = computed(
  () => props.candidates.find((c) => c.media_id === picked.value) ?? null,
)
const confirmedCandidate = computed(
  () => props.candidates.find((c) => c.media_id === confirmedId.value) ?? null,
)

function safetyTone(safety?: string): string {
  if (safety === 'PASS' || safety === 'PASSED') return 'ok'
  if (safety === 'REVIEW') return 'warn'
  if (safety === 'BLOCK' || safety === 'BLOCKED') return 'danger'
  return 'muted'
}

/** 把「体积不符：库里 785552B，实际 770800B」翻成人话。 */
function staleText(cand: ShotCandidate): string {
  const raw = cand.stale_reason || ''
  if (raw.includes('体积不符') || raw.includes('哈希')) return '与记录不一致，可能被同名图片覆盖过'
  if (raw.includes('缺失') || raw.includes('不可读')) return '文件已丢失'
  return '文件已失效'
}

function pick(mediaId: string): void {
  const target = props.candidates.find((c) => c.media_id === mediaId)
  if (!target || target.broken || target.usable === false) return
  picked.value = mediaId
  emit('select', mediaId)
}

function startReject(mediaId: string): void {
  rejectTarget.value = mediaId
  rejectReason.value = ''
}

function submitReject(): void {
  if (!rejectTarget.value || !rejectReason.value.trim()) return
  // 驳回的正是当前选中项：本地立刻取消选中，避免「已选」还挂在已驳回的图上
  if (picked.value === rejectTarget.value) picked.value = null
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
        <span class="mono tiny dim">
          {{ usableCount }} 张可用<template v-if="brokenCandidates.length">
            · {{ brokenCandidates.length }} 张已失效</template>
        </span>
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
      v-if="!healthyCandidates.length && !brokenCandidates.length"
      compact
      icon="▣"
      title="还没有候选图"
      description="在上方选择镜头并生成分镜图，候选会出现在这里。"
    />
    <EmptyState
      v-else-if="!healthyCandidates.length"
      compact
      icon="▣"
      title="可用候选已全部失效"
      description="这些图片的文件早前被同名图片覆盖，已自动停用；重新生成候选图即可。"
    />

    <div v-else class="cp-grid">
      <figure
        v-for="cand in healthyCandidates"
        :key="cand.media_id"
        class="cp-item"
        :class="{
          picked: picked === cand.media_id,
          confirmed: confirmedId === cand.media_id,
          rejected: cand.usable === false,
        }"
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
            <span
              v-if="cand.safety"
              class="badge badge-dot"
              :class="`badge-${safetyTone(cand.safety)}`"
              :title="'内容安全检查'"
            >
              {{ safetyLabel(cand.safety) }}
            </span>
          </div>
          <div class="tiny dim">{{ relativeTime(cand.created_at) }}</div>
          <p v-if="cand.rejected_reason" class="cp-reason tiny">{{ cand.rejected_reason }}</p>
          <div class="cp-actions" @click.stop>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="busy || cand.usable === false"
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

    <div v-if="brokenCandidates.length" class="cp-broken">
      <button type="button" class="btn btn-sm btn-ghost" @click="showBroken = !showBroken">
        {{ showBroken ? '收起失效候选' : `查看 ${brokenCandidates.length} 张已失效候选` }}
      </button>
      <p v-if="!showBroken" class="tiny dim">
        文件与记录不一致（早前被同名图片覆盖），已自动停用；重新生成即可，无需手动处理。
      </p>
      <ul v-else class="cp-broken-list">
        <li v-for="cand in brokenCandidates" :key="cand.media_id" class="tiny dim">
          <span class="mono">v{{ cand.version ?? '—' }}</span>
          <span>· {{ staleText(cand) }}</span>
          <span v-if="cand.created_at"> · {{ relativeTime(cand.created_at) }}</span>
        </li>
      </ul>
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
  gap: var(--space-3);
}
.cp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.cp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(142px, 1fr));
  gap: var(--space-3);
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
  border-color: rgba(201, 116, 58, 0.6);
  box-shadow: 0 0 0 1px rgba(201, 116, 58, 0.35);
}
.cp-item.confirmed {
  border-color: rgba(111, 191, 147, 0.55);
}
/* 已驳回：还能看、能恢复，但不能再选 */
.cp-item.rejected {
  opacity: 0.55;
  cursor: not-allowed;
}
.cp-item.rejected:hover {
  transform: none;
  border-color: var(--line);
}
.cp-broken {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: 10px;
  border-top: 1px dashed var(--line);
}
.cp-broken-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.cp-thumb {
  position: relative;
}
.cp-flag {
  position: absolute;
  top: 6px;
  left: 6px;
  font-size: var(--fs-11);
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--line);
  background: rgba(6, 5, 4, 0.76);
}
.cp-flag.ok {
  color: var(--ok);
  border-color: rgba(111, 191, 147, 0.5);
}
.cp-flag.accent {
  color: var(--accent-2);
  border-color: rgba(201, 116, 58, 0.5);
}
.cp-flag.danger {
  left: auto;
  right: 6px;
  color: var(--danger);
  border-color: rgba(224, 122, 106, 0.5);
}
.cp-cap {
  padding: var(--space-2) var(--space-3) 10px;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.cp-reason {
  color: var(--warn);
}
.cp-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.cp-reject {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px dashed var(--line-strong);
  border-radius: var(--radius-sm);
  background: rgba(243, 237, 227, 0.02);
}
.cp-compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-1);
}
.cp-compare-label {
  margin-bottom: 6px;
}
</style>
