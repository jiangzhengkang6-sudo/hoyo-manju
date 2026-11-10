<script setup lang="ts">
/** 任务中心（API.md §10、§10.1）：SSE 实时更新 + 断线降级轮询。 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useJobsStore } from '@/stores/jobs'
import { useToastStore } from '@/stores/toast'
import { workspaceEventsUrl } from '@/api/events'
import { JOB_STATUS_LABEL, JOB_TYPE_LABEL, isTerminalJobStatus, jobTypeLabel } from '@/api/jobs'
import { formatCents } from '@/api/cost'
import { providerStatusLabel, formatDateTime, formatDuration } from '@/utils/format'
import { useSse } from '@/composables/useSse'
import { usePolling } from '@/composables/usePolling'
import JobBadge from '@/components/JobBadge.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import type { Job, JobStatus } from '@/types'

const router = useRouter()
const store = useJobsStore()
const toast = useToastStore()
const { items, total, page, pageSize, filters, loading, error, detail, providerCalls, detailLoading } =
  storeToRefs(store)

const busyId = ref<string | null>(null)
const showDetail = ref(false)

const STATUS_OPTIONS: JobStatus[] = [
  'CREATED',
  'QUEUED',
  'RUNNING',
  'WAITING_CONFIRMATION',
  'SUCCEEDED',
  'FAILED',
  'RETRYING',
  'CANCELLING',
  'CANCELLED',
  'EXPIRED',
]

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / (pageSize.value || 20))))

const sse = useSse({
  url: (cursor) => workspaceEventsUrl(cursor),
  storageKey: 'hoyomanju:last_event_id_workspace',
  degradeAfter: 2,
  onEvent: (event) => {
    if (event.type.startsWith('job.')) store.applyEvent(event.data)
  },
})

const poller = usePolling(
  async () => {
    await store.load()
  },
  { intervalMs: 4000, immediate: false, shouldRun: () => sse.degraded.value },
)

const sseLabel = computed(() => {
  if (sse.degraded.value) return '实时已降级 · 轮询兜底'
  switch (sse.state.value) {
    case 'live':
      return '实时连接中'
    case 'connecting':
      return '连接中…'
    case 'reconnecting':
      return '重连中…'
    case 'error':
      return '连接异常'
    default:
      return '未连接'
  }
})

const sseTone = computed(() => {
  if (sse.degraded.value) return 'warn'
  return sse.state.value === 'live' ? 'ok' : 'muted'
})

onMounted(async () => {
  await store.load(true)
  sse.start()
  poller.start()
})

async function openDetail(job: Job): Promise<void> {
  showDetail.value = true
  await store.loadDetail(job.id)
}

async function retry(job: Job): Promise<void> {
  busyId.value = job.id
  try {
    const created = await store.retry(job.id)
    if (created) toast.success('已提交重试', created.id)
  } catch {
    /* api 层已提示 */
  } finally {
    busyId.value = null
  }
}

async function cancel(job: Job): Promise<void> {
  busyId.value = job.id
  try {
    const note = await store.cancel(job.id)
    toast.info('取消请求已提交', note ?? undefined)
  } catch {
    /* api 层已提示 */
  } finally {
    busyId.value = null
  }
}

function canCancel(job: Job): boolean {
  return (
    job.cancelable !== false &&
    !isTerminalJobStatus(job.status) &&
    job.status !== 'CANCELLED'
  )
}

function progressOf(job: Job): number {
  return Math.max(0, Math.min(100, job.progress ?? 0))
}

function barTone(job: Job): string {
  if (job.status === 'FAILED' || job.status === 'EXPIRED') return 'danger'
  if (job.status === 'SUCCEEDED') return 'ok'
  if (job.status === 'WAITING_CONFIRMATION' || job.status === 'QUEUED') return 'warn'
  return ''
}

function goEpisode(job: Job): void {
  if (!job.episode_id) return
  void router.push({ name: 'episode-story', params: { episodeId: job.episode_id } })
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">任务中心</h1>
        <p class="muted small">
          所有异步任务（剧本 / 分镜图 / 视频 / 配音 / 合成 / 导出）的实时状态、成本与重试入口。
        </p>
      </div>
      <div class="row-wrap">
        <span class="badge badge-dot" :class="`badge-${sseTone}`">{{ sseLabel }}</span>
        <span class="badge badge-info">执行中 {{ store.runningCount }}</span>
        <span class="badge badge-warn">排队 {{ store.queuedCount }}</span>
        <span class="badge badge-danger">失败 {{ store.failedCount }}</span>
        <button type="button" class="btn btn-ghost" :disabled="loading" @click="store.load()">刷新</button>
      </div>
    </header>

    <section class="card">
      <div class="card-body filter-bar">
        <div class="field">
          <label class="label" for="f-status">状态</label>
          <select
            id="f-status"
            class="select"
            :value="filters.status ?? ''"
            @change="store.setFilter({ status: ($event.target as HTMLSelectElement).value || undefined })"
          >
            <option value="">全部状态</option>
            <option v-for="status in STATUS_OPTIONS" :key="status" :value="status">
              {{ JOB_STATUS_LABEL[status] }}
            </option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="f-type">类型</label>
          <select
            id="f-type"
            class="select"
            :value="filters.type ?? ''"
            @change="store.setFilter({ type: ($event.target as HTMLSelectElement).value || undefined })"
          >
            <option value="">全部类型</option>
            <option v-for="(label, key) in JOB_TYPE_LABEL" :key="key" :value="key">{{ label }}</option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="f-episode">剧集 ID</label>
          <input
            id="f-episode"
            class="input mono"
            :value="filters.episode_id ?? ''"
            placeholder="epi_..."
            @change="store.setFilter({ episode_id: ($event.target as HTMLInputElement).value || undefined })"
          />
        </div>
        <div class="field">
          <label class="label" for="f-page-size">每页</label>
          <select
            id="f-page-size"
            class="select"
            :value="String(pageSize)"
            @change="store.setFilter({ page_size: Number(($event.target as HTMLSelectElement).value) })"
          >
            <option value="10">10</option>
            <option value="20">20</option>
            <option value="50">50</option>
          </select>
        </div>
      </div>
    </section>

    <SkeletonBlock v-if="loading && !items.length" :rows="6" :height="16" />

    <ErrorState v-else-if="error && !items.length" :error="error" context="加载任务列表" @retry="store.load()" />

    <EmptyState
      v-else-if="!items.length"
      icon="≡"
      title="没有符合条件的任务"
      description="调整筛选条件，或先去工作台提交一个生成任务。"
    >
      <button type="button" class="btn btn-ghost btn-sm" @click="store.setFilter({ status: undefined, type: undefined, episode_id: undefined })">
        清除筛选
      </button>
    </EmptyState>

    <section v-else class="card">
      <table class="table">
        <thead>
          <tr>
            <th>任务</th>
            <th style="width: 96px">状态</th>
            <th style="width: 168px">进度</th>
            <th style="width: 150px">供应商 / 模型</th>
            <th style="width: 90px">成本</th>
            <th style="width: 130px">创建时间</th>
            <th style="width: 190px" />
          </tr>
        </thead>
        <tbody>
          <tr v-for="job in items" :key="job.id">
            <td>
              <div class="job-cell">
                <div class="row-wrap">
                  <span class="badge badge-accent">{{ jobTypeLabel(job.type) }}</span>
                  <span v-if="job.attempt && job.attempt > 1" class="badge badge-warn">第 {{ job.attempt }} 次</span>
                </div>
                <span class="mono tiny dim-2 ellipsis">{{ job.id }}</span>
                <button
                  v-if="job.episode_id"
                  type="button"
                  class="link tiny"
                  @click="goEpisode(job)"
                >
                  {{ job.episode_id }}
                </button>
              </div>
            </td>
            <td><JobBadge :status="job.status" /></td>
            <td>
              <div class="progress">
                <div class="progress-bar" :class="barTone(job)" :style="{ width: `${progressOf(job)}%` }" />
              </div>
              <span class="mono tiny dim">{{ progressOf(job) }}%</span>
            </td>
            <td class="tiny dim">
              <div class="mono ellipsis">{{ job.provider || '—' }}</div>
              <div class="mono ellipsis">{{ job.model || '—' }}</div>
            </td>
            <td class="mono tiny">
              {{ formatCents(job.cost_settled_cents || job.cost_reserved_cents) }}
            </td>
            <td class="mono tiny dim">{{ formatDateTime(job.created_at) }}</td>
            <td>
              <div class="row" style="justify-content: flex-end">
                <button type="button" class="btn btn-sm btn-ghost" @click="openDetail(job)">详情</button>
                <button
                  v-if="job.status === 'FAILED' || job.status === 'EXPIRED'"
                  type="button"
                  class="btn btn-sm"
                  :disabled="busyId === job.id"
                  @click="retry(job)"
                >
                  重试
                </button>
                <button
                  v-if="canCancel(job)"
                  type="button"
                  class="btn btn-sm btn-ghost"
                  :disabled="busyId === job.id"
                  @click="cancel(job)"
                >
                  取消
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <div class="card-foot pager">
        <span class="tiny dim mono">共 {{ total }} 条 · 第 {{ page }} / {{ totalPages }} 页</span>
        <div class="row">
          <button type="button" class="btn btn-sm btn-ghost" :disabled="page <= 1" @click="store.setPage(page - 1)">
            上一页
          </button>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="page >= totalPages"
            @click="store.setPage(page + 1)"
          >
            下一页
          </button>
        </div>
      </div>
    </section>

    <ModalDialog :open="showDetail" title="任务详情" :width="720" @close="showDetail = false; store.closeDetail()">
      <SkeletonBlock v-if="detailLoading && !detail" :rows="6" />
      <template v-else-if="detail">
        <div class="row-wrap">
          <JobBadge :status="detail.status" />
          <span class="badge badge-accent">{{ jobTypeLabel(detail.type) }}</span>
          <span class="mono tiny dim">{{ detail.id }}</span>
        </div>

        <div class="progress progress-lg">
          <div class="progress-bar" :class="barTone(detail)" :style="{ width: `${progressOf(detail)}%` }" />
        </div>

        <dl class="kv">
          <dt>工作流</dt>
          <dd class="mono">{{ detail.workflow_run_id || '—' }}</dd>
          <dt>剧集</dt>
          <dd class="mono">{{ detail.episode_id || '—' }}</dd>
          <dt>阶段</dt>
          <dd class="mono">{{ detail.stage || '—' }}</dd>
          <dt>供应商 / 模型</dt>
          <dd class="mono">{{ detail.provider || '—' }} / {{ detail.model || '—' }}</dd>
          <dt>尝试次数</dt>
          <dd class="mono">{{ detail.attempt ?? 0 }} / {{ detail.max_attempts ?? '—' }}</dd>
          <dt>幂等键</dt>
          <dd class="mono tiny">{{ detail.idempotency_key || '—' }}</dd>
          <dt>成本</dt>
          <dd class="mono">
            预留 {{ formatCents(detail.cost_reserved_cents) }} · 结算 {{ formatCents(detail.cost_settled_cents) }}
          </dd>
          <dt>耗时</dt>
          <dd class="mono">
            {{ detail.duration_ms ? formatDuration(detail.duration_ms) : '—' }}
          </dd>
          <dt>父任务</dt>
          <dd class="mono tiny">{{ detail.parent_job_id || '—' }}</dd>
          <dt>创建 / 开始 / 结束</dt>
          <dd class="mono tiny">
            {{ formatDateTime(detail.created_at) }} · {{ formatDateTime(detail.started_at) }} ·
            {{ formatDateTime(detail.finished_at) }}
          </dd>
        </dl>

        <div v-if="detail.error_message" class="detail-error">
          <span class="badge badge-danger mono">{{ detail.error_code || 'ERROR' }}</span>
          <p class="small">{{ detail.error_message }}</p>
        </div>

        <div v-if="detail.input_summary" class="detail-block">
          <span class="section-title">输入摘要</span>
          <pre class="pre-wrap mono tiny">{{ JSON.stringify(detail.input_summary, null, 2) }}</pre>
        </div>

        <div v-if="detail.result_ref" class="detail-block">
          <span class="section-title">结果引用</span>
          <pre class="pre-wrap mono tiny">{{ JSON.stringify(detail.result_ref, null, 2) }}</pre>
        </div>

        <div class="detail-block">
          <span class="section-title">供应商调用（{{ providerCalls.length }}）</span>
          <EmptyState v-if="!providerCalls.length" compact icon="⇄" title="暂无调用记录" />
          <table v-else class="table">
            <thead>
              <tr>
                <th>供应商</th>
                <th>模型</th>
                <th>状态</th>
                <th>成本</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(call, i) in providerCalls" :key="call.id ?? i">
                <td class="mono tiny">{{ call.provider || '—' }}</td>
                <td class="mono tiny">{{ call.model || '—' }}</td>
                <td>
                  <span class="badge" :class="call.status === 'SUCCEEDED' ? 'badge-ok' : call.status === 'FAILED' ? 'badge-danger' : 'badge-info'">
                    {{ providerStatusLabel(call.status) }}
                  </span>
                </td>
                <td class="mono tiny">
                  {{ formatCents(call.settled_cost_cents ?? call.estimated_cost_cents) }}
                </td>
                <td class="mono tiny dim">{{ formatDateTime(call.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showDetail = false; store.closeDetail()">关闭</button>
        <button
          v-if="detail && (detail.status === 'FAILED' || detail.status === 'EXPIRED')"
          type="button"
          class="btn btn-primary"
          :disabled="busyId === detail.id"
          @click="retry(detail)"
        >
          重试任务
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.filter-bar {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: var(--gap);
}
.job-cell {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}
.link {
  background: transparent;
  border: 0;
  padding: 0;
  color: var(--accent);
  cursor: pointer;
  text-align: left;
}
.link:hover {
  text-decoration: underline;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}
.detail-error {
  border: 1px solid rgba(224, 122, 106, 0.35);
  background: var(--danger-soft);
  border-radius: var(--radius-sm);
  padding: var(--space-3) var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.detail-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.detail-block pre {
  margin: 0;
  padding: var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-1);
  max-height: 220px;
  overflow: auto;
}
</style>
