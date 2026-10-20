<script setup lang="ts">
/**
 * 行内任务卡：状态徽章 + 进度条 + 重试 / 取消。
 * 用于各 gate 的「生成中」反馈，SSE 与轮询都会更新传入的 job 对象。
 */
import { computed } from 'vue'
import { jobStatusLabel, jobStatusTone } from '@/api/jobs'
import { formatCents } from '@/api/cost'
import { formatDateTime, formatDuration } from '@/utils/format'
import JobBadge from './JobBadge.vue'
import type { Job } from '@/types'

const props = withDefaults(
  defineProps<{
    job: Job
    /** 是否显示重试按钮（默认显示，失败时才有意义）。 */
    allowRetry?: boolean
    /** 是否显示取消按钮。 */
    allowCancel?: boolean
    busy?: boolean
  }>(),
  { allowRetry: true, allowCancel: true, busy: false },
)

const emit = defineEmits<{ retry: [id: string]; cancel: [id: string] }>()

const progress = computed(() => Math.max(0, Math.min(100, props.job.progress ?? 0)))
const tone = computed(() => jobStatusTone(props.job.status))
const barClass = computed(() =>
  tone.value === 'ok' ? 'ok' : tone.value === 'danger' ? 'danger' : tone.value === 'warn' ? 'warn' : '',
)
const canRetry = computed(
  () => props.allowRetry && (props.job.status === 'FAILED' || props.job.status === 'EXPIRED'),
)
const canCancel = computed(
  () =>
    props.allowCancel &&
    props.job.cancelable !== false &&
    ['CREATED', 'QUEUED', 'RUNNING', 'RETRYING', 'WAITING_CONFIRMATION'].includes(props.job.status),
)
</script>

<template>
  <div class="job-inline">
    <div class="job-top">
      <div class="row" style="min-width: 0">
        <JobBadge :status="job.status" />
        <span class="job-id mono dim-2 tiny ellipsis">{{ job.id }}</span>
      </div>
      <span class="job-progress mono tiny">{{ jobStatusLabel(job.status) }} · {{ progress }}%</span>
    </div>

    <div class="progress">
      <div class="progress-bar" :class="barClass" :style="{ width: `${progress}%` }" />
    </div>

    <div class="job-meta tiny dim">
      <span v-if="job.stage">阶段 <span class="mono">{{ job.stage }}</span></span>
      <span v-if="job.provider || job.model">
        <span class="mono">{{ job.provider || '—' }}</span> / <span class="mono">{{ job.model || '—' }}</span>
      </span>
      <span v-if="job.attempt">第 <span class="mono">{{ job.attempt }}</span>/{{ job.max_attempts ?? '?' }} 次</span>
      <span v-if="job.cost_settled_cents || job.cost_reserved_cents">
        成本 <span class="mono">{{ formatCents(job.cost_settled_cents || job.cost_reserved_cents) }}</span>
      </span>
      <span v-if="job.duration_ms">耗时 <span class="mono">{{ formatDuration(job.duration_ms) }}</span></span>
      <span v-else-if="job.started_at">开始 <span class="mono">{{ formatDateTime(job.started_at) }}</span></span>
    </div>

    <p v-if="job.error_message" class="job-error tiny">
      <span class="mono">{{ job.error_code || 'ERROR' }}</span> · {{ job.error_message }}
    </p>

    <div v-if="canRetry || canCancel" class="job-actions">
      <button
        v-if="canRetry"
        type="button"
        class="btn btn-sm"
        :disabled="busy"
        @click="emit('retry', job.id)"
      >
        重试
      </button>
      <button
        v-if="canCancel"
        type="button"
        class="btn btn-sm btn-ghost"
        :disabled="busy"
        @click="emit('cancel', job.id)"
      >
        取消
      </button>
    </div>
  </div>
</template>

<style scoped>
.job-inline {
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.job-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.job-id {
  max-width: 190px;
}
.job-progress {
  color: var(--text-2);
  flex: 0 0 auto;
}
.job-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
}
.job-error {
  color: var(--danger);
  word-break: break-word;
}
.job-actions {
  display: flex;
  gap: 8px;
}
</style>
