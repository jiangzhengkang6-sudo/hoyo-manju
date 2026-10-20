<script setup lang="ts">
/**
 * 成本与记录（PRD §5.1 页面清单第 11 项 / API.md §13）。
 *
 * 四块内容：
 *  1. 配额与总览 —— `/quota` + `/cost/summary` + `/stats/workspace`
 *  2. 成本流水   —— `/cost/ledger`（类型 / 剧集筛选 + 后端分页）
 *  3. 审计日志   —— `/audit`
 *  4. 运行记录   —— `/jobs`（筛出失败与重试中的任务）
 *
 * 金额一律是后端「分」的整数，展示时统一走 `formatCents*`（除以 100，两位小数）；
 * 所有配额与限制数值均来自接口，前端不写死任何额度。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, toApiError } from '@/api/errors'
import {
  LEDGER_KIND_OPTIONS,
  fetchCostSummary,
  fetchLedgerPage,
  fetchQuota,
  formatCents,
  formatCentsSigned,
  ledgerKindLabel,
  ledgerKindTone,
  stageCostLabel,
} from '@/api/cost'
import {
  auditActionLabel,
  auditDetailText,
  auditTargetLabel,
  fetchAuditLogs,
  fetchWorkspaceStats,
} from '@/api/admin'
import { episodesApi } from '@/api/episodes'
import { projectsApi, asList } from '@/api/projects'
import { JOB_STATUS_LABEL, fetchJobs, jobStatusTone, jobTypeLabel } from '@/api/jobs'
import { formatDateTime, formatDuration } from '@/utils/format'
import { usePolling } from '@/composables/usePolling'
import StatTile from '@/components/StatTile.vue'
import QuotaMeter from '@/components/QuotaMeter.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import type { AuditLogEntry, CostLedgerEntry, CostSummary, Job, Quota, WorkspaceStats } from '@/types'

const router = useRouter()

/** 未知值 → 展示文本（后端可选字段用）。 */
function textOf(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

/* ------------------------------------------------------------------ *
 * 1. 配额与总览
 * ------------------------------------------------------------------ */

const quota = ref<Quota | null>(null)
const summary = ref<CostSummary | null>(null)
const stats = ref<WorkspaceStats | null>(null)
const overviewLoading = ref(false)
const overviewError = ref<ApiError | null>(null)

const hasOverview = computed(() => Boolean(quota.value || summary.value || stats.value))

/** 汇总：按阶段分布（只取后端已归集的 SETTLE）。 */
const stageBuckets = computed(() => {
  const rows = summary.value?.by_stage ?? []
  const max = rows.reduce((m, r) => Math.max(m, r.cents ?? 0), 0)
  return rows.map((bucket) => ({
    label: stageCostLabel(bucket.stage),
    cents: bucket.cents ?? 0,
    width: max > 0 ? Math.max(2, ((bucket.cents ?? 0) / max) * 100) : 0,
  }))
})

async function loadOverview(): Promise<void> {
  overviewLoading.value = true
  overviewError.value = null
  // 三个接口互不依赖：并行拉取，部分失败仍渲染已拿到的部分。
  const results = await Promise.allSettled([
    fetchQuota(),
    fetchCostSummary(),
    fetchWorkspaceStats(),
  ])
  const [quotaRes, summaryRes, statsRes] = results
  if (quotaRes.status === 'fulfilled') quota.value = quotaRes.value
  if (summaryRes.status === 'fulfilled') summary.value = summaryRes.value
  if (statsRes.status === 'fulfilled') stats.value = statsRes.value
  const failed = results.find((r) => r.status === 'rejected')
  overviewError.value = failed && failed.status === 'rejected' ? toApiError(failed.reason) : null
  overviewLoading.value = false
}

/* ------------------------------------------------------------------ *
 * 2. 成本流水
 * ------------------------------------------------------------------ */

const ledgerItems = ref<CostLedgerEntry[]>([])
const ledgerTotal = ref(0)
const ledgerPage = ref(1)
const ledgerPageSize = ref(20)
const ledgerKind = ref('')
const ledgerEpisodeId = ref('')
const ledgerLoading = ref(false)
const ledgerLoaded = ref(false)
const ledgerError = ref<ApiError | null>(null)

const ledgerTotalPages = computed(() =>
  Math.max(1, Math.ceil(ledgerTotal.value / (ledgerPageSize.value || 1))),
)

/** 剧集下拉：从项目接口拉项目，再逐个拉剧集（后端无全局剧集列表接口）。 */
const episodeOptions = ref<{ id: string; label: string }[]>([])
const episodeOptionsLoading = ref(false)
const episodeOptionsError = ref<ApiError | null>(null)

async function loadEpisodeOptions(): Promise<void> {
  episodeOptionsLoading.value = true
  episodeOptionsError.value = null
  try {
    const projects = asList(await projectsApi.list())
    const groups = await Promise.all(
      projects.map(async (project) => {
        try {
          const episodes = await episodesApi.list(project.id)
          return episodes.map((episode) => ({
            id: episode.id,
            label: `${project.name} · ${
              episode.episode_no != null ? `EP${String(episode.episode_no).padStart(2, '0')} ` : ''
            }${episode.title}`,
          }))
        } catch {
          // 单个项目的剧集拉取失败不影响其他项目
          return [] as { id: string; label: string }[]
        }
      }),
    )
    episodeOptions.value = groups.flat()
  } catch (err) {
    episodeOptionsError.value = toApiError(err)
    episodeOptions.value = []
  } finally {
    episodeOptionsLoading.value = false
  }
}

async function loadLedger(): Promise<void> {
  ledgerLoading.value = true
  ledgerError.value = null
  try {
    const page = await fetchLedgerPage({
      kind: ledgerKind.value || undefined,
      episode_id: ledgerEpisodeId.value || undefined,
      page: ledgerPage.value,
      page_size: ledgerPageSize.value,
    })
    ledgerItems.value = page.items
    ledgerTotal.value = page.total
    ledgerPage.value = page.page
    ledgerPageSize.value = page.page_size
    ledgerLoaded.value = true
  } catch (err) {
    ledgerError.value = toApiError(err)
  } finally {
    ledgerLoading.value = false
  }
}

/** 筛选条件变化时回到第一页。 */
function applyLedgerFilter(): void {
  ledgerPage.value = 1
  void loadLedger()
}

function gotoLedgerPage(next: number): void {
  ledgerPage.value = Math.min(Math.max(1, next), ledgerTotalPages.value)
  void loadLedger()
}

/* ------------------------------------------------------------------ *
 * 3. 审计日志
 * ------------------------------------------------------------------ */

const auditItems = ref<AuditLogEntry[]>([])
const auditLimit = ref(100)
const auditLoading = ref(false)
const auditLoaded = ref(false)
const auditError = ref<ApiError | null>(null)

async function loadAudit(): Promise<void> {
  auditLoading.value = true
  auditError.value = null
  try {
    auditItems.value = await fetchAuditLogs({ limit: auditLimit.value })
    auditLoaded.value = true
  } catch (err) {
    auditError.value = toApiError(err)
  } finally {
    auditLoading.value = false
  }
}

/* ------------------------------------------------------------------ *
 * 4. 运行记录（失败 / 重试中的任务）
 * ------------------------------------------------------------------ */

/** 关注的任务状态：失败与重试中。 */
const FAILURE_STATUSES = ['FAILED', 'RETRYING'] as const

const failureJobs = ref<Job[]>([])
const failuresLoading = ref(false)
const failuresLoaded = ref(false)
const failuresError = ref<ApiError | null>(null)

/** 加载失败 / 重试任务列表；`silent` 用于轮询，避免重复弹 toast（错误仍由错误态展示）。 */
async function loadFailures(silent = false): Promise<void> {
  failuresLoading.value = true
  failuresError.value = null
  try {
    const results = await Promise.allSettled(
      FAILURE_STATUSES.map((status) =>
        fetchJobs({ status, page: 1, page_size: 20 }, { silent }),
      ),
    )
    const merged = results.flatMap((r) => (r.status === 'fulfilled' ? r.value.items : []))
    merged.sort((a, b) => Date.parse(b.created_at ?? '') - Date.parse(a.created_at ?? ''))
    const seen = new Set<string>()
    failureJobs.value = merged
      .filter((job) => (seen.has(job.id) ? false : seen.add(job.id)))
      .slice(0, 20)
    const failed = results.find((r) => r.status === 'rejected')
    failuresError.value = failed && failed.status === 'rejected' ? toApiError(failed.reason) : null
    failuresLoaded.value = true
  } catch (err) {
    failuresError.value = toApiError(err)
  } finally {
    failuresLoading.value = false
  }
}

/** 运行记录会随任务状态变化，做一次轻量轮询（静默模式，失败不弹 toast）。 */
const failuresPoller = usePolling(() => loadFailures(true), { intervalMs: 10000, immediate: false })

/** 跳到任务中心并带上该任务。 */
function openJob(jobId: string): void {
  void router.push({ name: 'jobs', query: { job_id: jobId } })
}

/* ------------------------------------------------------------------ *
 * 汇总刷新
 * ------------------------------------------------------------------ */

/** 只跟随用户可见的手动刷新，避免运行记录的轮询导致按钮文案闪烁。 */
const refreshing = computed(
  () => overviewLoading.value || ledgerLoading.value || auditLoading.value,
)

async function loadAll(): Promise<void> {
  await Promise.all([loadOverview(), loadLedger(), loadAudit(), loadFailures()])
}

onMounted(async () => {
  void loadEpisodeOptions()
  await loadAll()
  failuresPoller.start()
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">成本与记录</h1>
        <p class="muted small">
          配额与用量总览、成本流水（预留 / 结算 / 释放）、审计日志与失败任务运行记录。金额单位均为人民币元，
          数据来自后端 <span class="mono">/quota</span>、<span class="mono">/cost/*</span>、
          <span class="mono">/audit</span> 与 <span class="mono">/jobs</span>。
        </p>
      </div>
      <div class="row-wrap">
        <button type="button" class="btn btn-ghost" :disabled="refreshing" @click="loadAll">
          {{ refreshing ? '刷新中…' : '刷新全部' }}
        </button>
      </div>
    </header>

    <!-- 1. 配额与总览 -->
    <section class="card">
      <div class="card-head">
        <h2 class="title-md">配额与总览</h2>
        <span class="tiny dim mono">/quota · /cost/summary · /stats/workspace</span>
      </div>
      <div class="card-body">
        <SkeletonBlock v-if="overviewLoading && !hasOverview" variant="cards" :rows="4" />
        <ErrorState
          v-else-if="overviewError && !hasOverview"
          :error="overviewError"
          context="加载配额与总览"
          @retry="loadOverview"
        />
        <EmptyState
          v-else-if="!hasOverview"
          compact
          icon="¥"
          title="暂无配额与统计数据"
          description="后端未返回配额或工作区统计，请确认服务已启动。"
        />
        <template v-else>
          <ErrorState
            v-if="overviewError"
            compact
            :error="overviewError"
            context="刷新配额与总览"
            @retry="loadOverview"
          />
          <div class="overview-grid">
            <div class="stat-row">
              <StatTile
                label="剩余配额"
                :value="quota ? formatCents(quota.remaining_cents) : '—'"
                tone="accent"
                hint="可用余额"
              />
              <StatTile
                label="已用"
                :value="quota ? formatCents(quota.used_cents) : '—'"
                hint="已结算支出"
              />
              <StatTile
                label="已预留"
                :value="quota ? formatCents(quota.reserved_cents) : '—'"
                tone="warn"
                hint="进行中任务冻结"
              />
              <StatTile label="总配额" :value="quota ? formatCents(quota.limit_cents) : '—'" />
              <StatTile label="项目数" :value="stats?.projects ?? '—'" />
              <StatTile label="剧集数" :value="stats?.episodes ?? '—'" />
              <StatTile label="任务数" :value="stats?.jobs ?? '—'" />
              <StatTile label="素材数" :value="stats?.media ?? '—'" />
            </div>

            <div class="overview-side">
              <QuotaMeter :quota="quota" :loading="overviewLoading" />

              <div class="divider" />

              <div class="bucket-group">
                <div class="row-between">
                  <span class="section-title">累计结算</span>
                  <span class="mono tiny">{{ formatCents(summary?.total_cents) }}</span>
                </div>
                <div v-for="bucket in stageBuckets" :key="bucket.label" class="bucket">
                  <span class="bucket-label tiny">{{ bucket.label }}</span>
                  <div class="progress">
                    <div class="progress-bar" :style="{ width: `${bucket.width}%` }" />
                  </div>
                  <span class="mono tiny bucket-cents">{{ formatCents(bucket.cents) }}</span>
                </div>
                <p v-if="summary && !stageBuckets.length" class="tiny dim">后端未返回按阶段的成本归集。</p>
                <p v-if="!summary" class="tiny dim">成本汇总不可用（/cost/summary 未返回）。</p>
              </div>
            </div>
          </div>
        </template>
      </div>
    </section>

    <!-- 2. 成本流水 -->
    <section class="card">
      <div class="card-head">
        <h2 class="title-md">成本流水</h2>
        <span class="tiny dim mono">/cost/ledger</span>
      </div>

      <div class="card-body filter-bar">
        <div class="field">
          <label class="label" for="l-kind">类型</label>
          <select id="l-kind" v-model="ledgerKind" class="select" @change="applyLedgerFilter">
            <option value="">全部类型</option>
            <option v-for="kind in LEDGER_KIND_OPTIONS" :key="kind" :value="kind">
              {{ ledgerKindLabel(kind) }}（{{ kind }}）
            </option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="l-episode">剧集</label>
          <select
            id="l-episode"
            v-model="ledgerEpisodeId"
            class="select"
            :disabled="episodeOptionsLoading"
            @change="applyLedgerFilter"
          >
            <option value="">{{ episodeOptionsLoading ? '加载中…' : '全部剧集' }}</option>
            <option v-for="option in episodeOptions" :key="option.id" :value="option.id">
              {{ option.label }}
            </option>
          </select>
          <span v-if="episodeOptionsError" class="hint">
            剧集列表加载失败：{{ episodeOptionsError.message }}
            <span v-if="episodeOptionsError.traceId" class="mono">（trace_id {{ episodeOptionsError.traceId }}）</span>
          </span>
        </div>
        <div class="field">
          <label class="label" for="l-page-size">每页</label>
          <select id="l-page-size" v-model.number="ledgerPageSize" class="select" @change="applyLedgerFilter">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
        </div>
        <div class="field">
          <label class="label">筛选</label>
          <button
            type="button"
            class="btn btn-ghost"
            :disabled="!ledgerKind && !ledgerEpisodeId"
            @click="ledgerKind = ''; ledgerEpisodeId = ''; applyLedgerFilter()"
          >
            清除筛选
          </button>
        </div>
      </div>

      <SkeletonBlock v-if="ledgerLoading && !ledgerItems.length" class="pad-x" :rows="6" :height="16" />
      <ErrorState
        v-else-if="ledgerError && !ledgerItems.length"
        class="pad-x"
        :error="ledgerError"
        context="加载成本流水"
        @retry="loadLedger"
      />
      <EmptyState
        v-else-if="ledgerLoaded && !ledgerItems.length"
        compact
        icon="¥"
        title="没有符合条件的流水"
        description="调整筛选条件，或先去工作台提交一次生成任务。"
      />
      <ErrorState
        v-if="ledgerError && ledgerItems.length"
        class="pad-x"
        compact
        :error="ledgerError"
        context="刷新成本流水"
        @retry="loadLedger"
      />
      <table v-if="ledgerItems.length" class="table">
        <thead>
          <tr>
            <th style="width: 130px">时间</th>
            <th style="width: 100px">类型</th>
            <th style="width: 120px">金额</th>
            <th style="width: 110px">阶段</th>
            <th style="width: 190px">关联任务</th>
            <th>备注</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(entry, i) in ledgerItems" :key="entry.id ?? i">
            <td class="mono tiny dim">{{ formatDateTime(entry.created_at) }}</td>
            <td>
              <span class="badge" :class="`badge-${ledgerKindTone(entry.kind)}`">
                {{ ledgerKindLabel(entry.kind) }}
              </span>
            </td>
            <td
              class="mono amount"
              :class="(entry.amount_cents ?? 0) < 0 ? 'amount-neg' : 'amount-pos'"
              :title="entry.amount_cents != null ? `${entry.amount_cents} 分` : ''"
            >
              {{ formatCentsSigned(entry.amount_cents) }}
            </td>
            <td class="tiny dim">{{ stageCostLabel(entry.stage) }}</td>
            <td>
              <button
                v-if="entry.job_id"
                type="button"
                class="link mono tiny"
                :title="String(entry.job_id)"
                @click="openJob(String(entry.job_id))"
              >
                {{ String(entry.job_id) }}
              </button>
              <span v-else class="tiny dim">—</span>
            </td>
            <td class="tiny muted pre-wrap">{{ textOf(entry.note) }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="ledgerItems.length" class="card-foot pager">
        <span class="tiny dim mono">
          共 {{ ledgerTotal }} 条 · 第 {{ ledgerPage }} / {{ ledgerTotalPages }} 页
        </span>
        <div class="row">
          <span class="legend tiny dim">
            <span class="amount-pos mono">+</span> 支出（预留 / 结算）·
            <span class="amount-neg mono">−</span> 释放
          </span>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="ledgerPage <= 1 || ledgerLoading"
            @click="gotoLedgerPage(ledgerPage - 1)"
          >
            上一页
          </button>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="ledgerPage >= ledgerTotalPages || ledgerLoading"
            @click="gotoLedgerPage(ledgerPage + 1)"
          >
            下一页
          </button>
        </div>
      </div>
    </section>

    <!-- 3. 审计日志 -->
    <section class="card">
      <div class="card-head">
        <h2 class="title-md">审计日志</h2>
        <div class="row">
          <span class="tiny dim mono">{{ auditItems.length }} 条</span>
          <select v-model.number="auditLimit" class="select select-sm" @change="loadAudit">
            <option :value="50">最近 50 条</option>
            <option :value="100">最近 100 条</option>
            <option :value="200">最近 200 条</option>
          </select>
          <button type="button" class="btn btn-sm btn-ghost" :disabled="auditLoading" @click="loadAudit">
            刷新
          </button>
        </div>
      </div>

      <SkeletonBlock v-if="auditLoading && !auditItems.length" class="pad-x" :rows="6" :height="16" />
      <ErrorState
        v-else-if="auditError && !auditItems.length"
        class="pad-x"
        :error="auditError"
        context="加载审计日志"
        @retry="loadAudit"
      />
      <EmptyState
        v-else-if="auditLoaded && !auditItems.length"
        compact
        icon="≡"
        title="暂无审计记录"
        description="删除 / 导出 / 配额 / 供应商变更等操作会记录在此。"
      />
      <ErrorState
        v-if="auditError && auditItems.length"
        class="pad-x"
        compact
        :error="auditError"
        context="刷新审计日志"
        @retry="loadAudit"
      />
      <table v-if="auditItems.length" class="table">
        <thead>
          <tr>
            <th style="width: 130px">时间</th>
            <th style="width: 160px">动作</th>
            <th style="width: 120px">目标类型</th>
            <th style="width: 210px">目标 ID</th>
            <th style="width: 150px">操作者</th>
            <th>明细</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(entry, i) in auditItems" :key="entry.id ?? i">
            <td class="mono tiny dim">{{ formatDateTime(entry.created_at) }}</td>
            <td>
              <span class="badge badge-info" :title="entry.action ?? ''">
                {{ auditActionLabel(entry.action) }}
              </span>
            </td>
            <td class="tiny dim">{{ auditTargetLabel(entry.target_type) }}</td>
            <td class="mono tiny ellipsis" :title="entry.target_id ?? ''">
              {{ entry.target_id || '—' }}
            </td>
            <td class="mono tiny dim">{{ entry.actor_id || '系统' }}</td>
            <td class="tiny muted pre-wrap">{{ auditDetailText(entry.detail) || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 4. 运行记录 -->
    <section class="card">
      <div class="card-head">
        <h2 class="title-md">运行记录 · 失败与重试</h2>
        <div class="row">
          <span class="tiny dim mono">/jobs?status=FAILED · RETRYING</span>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="failuresLoading"
            @click="loadFailures()"
          >
            刷新
          </button>
        </div>
      </div>

      <SkeletonBlock v-if="failuresLoading && !failuresLoaded" class="pad-x" :rows="5" :height="16" />
      <ErrorState
        v-else-if="failuresError && !failureJobs.length"
        class="pad-x"
        :error="failuresError"
        context="加载运行记录"
        @retry="loadFailures()"
      />
      <EmptyState
        v-else-if="failuresLoaded && !failureJobs.length"
        compact
        icon="✓"
        title="近期没有失败或重试中的任务"
        description="出现失败任务时会在这里列出，点击可跳到任务中心查看详情。"
      />
      <ErrorState
        v-if="failuresError && failureJobs.length"
        class="pad-x"
        compact
        :error="failuresError"
        context="刷新运行记录"
        @retry="loadFailures()"
      />
      <table v-if="failureJobs.length" class="table">
        <thead>
          <tr>
            <th style="width: 200px">任务 ID</th>
            <th style="width: 150px">类型</th>
            <th style="width: 100px">状态</th>
            <th style="width: 150px">错误码</th>
            <th style="width: 110px">重试次数</th>
            <th style="width: 100px">耗时</th>
            <th style="width: 130px">时间</th>
            <th style="width: 80px" />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="job in failureJobs"
            :key="job.id"
            class="clickable"
            :title="job.error_message ?? ''"
            @click="openJob(job.id)"
          >
            <td class="mono tiny ellipsis">{{ job.id }}</td>
            <td class="tiny">{{ jobTypeLabel(job.type) }}</td>
            <td>
              <span class="badge" :class="`badge-${jobStatusTone(job.status)}`">
                {{ JOB_STATUS_LABEL[job.status] ?? job.status }}
              </span>
            </td>
            <td>
              <span v-if="job.error_code" class="badge badge-danger mono">{{ job.error_code }}</span>
              <span v-else class="tiny dim">—</span>
            </td>
            <td class="mono tiny">{{ job.attempt ?? 0 }} / {{ job.max_attempts ?? '—' }}</td>
            <td class="mono tiny">{{ job.duration_ms ? formatDuration(job.duration_ms) : '—' }}</td>
            <td class="mono tiny dim">{{ formatDateTime(job.created_at) }}</td>
            <td>
              <button type="button" class="btn btn-sm btn-ghost" @click.stop="openJob(job.id)">
                查看
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="failureJobs.length" class="card-foot pager">
        <span class="tiny dim">点击任意一行可跳到任务中心并定位该任务。</span>
        <button type="button" class="btn btn-sm btn-ghost" @click="router.push({ name: 'jobs' })">
          打开任务中心
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: var(--gap-lg);
  max-width: 1500px;
}
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.page-head p {
  margin-top: 4px;
  max-width: 720px;
}
.overview-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: var(--gap-lg);
  align-items: start;
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: var(--gap);
}
.overview-side {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
}
.bucket-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bucket {
  display: grid;
  grid-template-columns: minmax(72px, 96px) 1fr auto;
  align-items: center;
  gap: 8px;
}
.bucket-label {
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bucket-cents {
  color: var(--text-1);
}
.filter-bar {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--gap);
  border-bottom: 1px solid var(--line);
}
.pad-x {
  padding: 16px;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.legend {
  white-space: nowrap;
}
.amount {
  font-weight: 600;
}
.amount-pos {
  color: var(--warn);
}
.amount-neg {
  color: var(--ok);
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
.select-sm {
  width: auto;
  padding: 5px 26px 5px 8px;
  font-size: 12px;
}
@media (max-width: 1120px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }
}
</style>
