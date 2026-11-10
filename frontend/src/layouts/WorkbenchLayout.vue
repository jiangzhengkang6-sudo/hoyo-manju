<script setup lang="ts">
/**
 * 剧集工作台布局（PRD §5.2 五区）：
 * - 顶部：剧集名称、状态、总进度、累计成本、保存状态
 * - 左侧：生产阶段导航 + 门控状态 + 工作流节点进度
 * - 中部：当前阶段主工作区（RouterView）
 * - 右侧：智能体建议 + 错误提示 + 版本和操作日志（可折叠）
 * - 底部：任务进度 + 批量操作 + 下一步
 * 同时负责建立 SSE 实时连接（断线自动降级为轮询）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { STAGE_ROUTE, episodesApi, stageIndex, stageLabel } from '@/api/episodes'
import { GATE_LABEL, GATE_ROUTE, gateLabel } from '@/api/workflow'
import { formatCents } from '@/api/cost'
import { cancelJob as cancelJobRequest, isTerminalJobStatus, jobTypeLabel } from '@/api/jobs'
import { shotsApi, type ImageEstimate } from '@/api/shots'
import { storyApi, versionsApi } from '@/api/story'
import { videoApi } from '@/api/video'
import { audioApi } from '@/api/audio'
import { renderApi } from '@/api/render'
import { formatDateTime } from '@/utils/format'
import NodeProgress from '@/components/NodeProgress.vue'
import ErrorState from '@/components/ErrorState.vue'
import EmptyState from '@/components/EmptyState.vue'
import JobBadge from '@/components/JobBadge.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import type {
  EpisodeStage,
  Job,
  VersionDiff,
  VersionRecord,
  VideoQuote,
  WorkflowLogEntry,
} from '@/types'

const props = defineProps<{ episodeId: string }>()

const route = useRoute()
const router = useRouter()
const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()

const {
  episode,
  workflow,
  graph,
  logs,
  shots,
  episodeJobs,
  loading,
  error,
  workflowError,
  sseState,
  sseDegraded,
  pendingGate,
  shotCount,
  confirmedShotCount,
  totalDurationMs,
  jobSnapshots,
  watchedJobIds,
  saving,
  lastSavedAt,
  saveError,
} = storeToRefs(store)

/* ------------------------------------------------------------------ *
 * 1. 五区折叠状态（窄屏自动收起左右侧栏）
 * ------------------------------------------------------------------ */

const sideCollapsed = ref(false)
const railCollapsed = ref(false)
const narrow = ref(false)
/** 用户手动开合过右栏后，就不再让断点自动覆盖他的选择 */
const railTouched = ref(false)

let mediaQuery: MediaQueryList | null = null
let wideQuery: MediaQueryList | null = null

function onMediaChange(event: MediaQueryListEvent): void {
  narrow.value = event.matches
  sideCollapsed.value = event.matches
  // 窄屏放不下两栏，必须收起（用户手动展开仍可临时覆盖）
  if (event.matches) railCollapsed.value = true
}

function onWideChange(event: MediaQueryListEvent): void {
  if (!railTouched.value) railCollapsed.value = !event.matches
}

function toggleRail(): void {
  railTouched.value = true
  railCollapsed.value = !railCollapsed.value
}

function collapseRail(): void {
  railTouched.value = true
  railCollapsed.value = true
}

/* ------------------------------------------------------------------ *
 * 2. 顶部：标题、状态、总进度、累计成本、保存状态
 * ------------------------------------------------------------------ */

/** 顶部条实际高度，供左右侧栏 sticky 定位使用（标题换行时也能对齐）。 */
const topRef = ref<HTMLElement | null>(null)
const topHeight = ref(64)
const bottomRef = ref<HTMLElement | null>(null)
const bottomHeight = ref(112)
let resizeObserver: ResizeObserver | null = null
let bottomObserver: ResizeObserver | null = null

const copying = ref(false)
const showCopy = ref(false)
const copyTitle = ref('')
const starting = ref(false)

/** 尚未创建 workflow run 时，从顶部直接启动流水线（后端会自动跑到第一个门控）。 */
async function startWorkflow(): Promise<void> {
  if (starting.value) return
  starting.value = true
  try {
    const run = await store.startWorkflow()
    if (run) toast.success('工作流已启动', `当前节点 ${run.current_node ?? '—'}`)
  } catch {
    // 错误已由 api 层 toast 展示（含 trace_id）
  } finally {
    starting.value = false
  }
}

/** 生产阶段（左侧导航）。`stage` 为该阶段对应的剧集复核阶段，用于推算完成度。 */
interface StageDef {
  key: string
  label: string
  icon: string
  route: string
  stage: EpisodeStage
}

const STAGES: StageDef[] = [
  { key: 'story', label: '故事与剧本', icon: '✎', route: 'episode-story', stage: 'SCRIPT_REVIEW' },
  { key: 'characters', label: '角色与画风', icon: '☺', route: 'episode-characters', stage: 'ASSET_REVIEW' },
  { key: 'shots', label: '分镜画面', icon: '▦', route: 'episode-shots', stage: 'SHOT_REVIEW' },
  { key: 'video', label: '视频生成', icon: '▶', route: 'episode-video', stage: 'VIDEO_REVIEW' },
  { key: 'audio', label: '音频字幕', icon: '♪', route: 'episode-audio', stage: 'AUDIO_REVIEW' },
  { key: 'render', label: '预览与导出', icon: '⤓', route: 'episode-render', stage: 'PREVIEW_READY' },
]

/** 六个门控及其所属剧集阶段（与后端 GATE_LABEL / STAGE_ORDER 对齐）。 */
const GATE_LIST: { id: string; stage: EpisodeStage }[] = [
  { id: 'gate_script_confirmation', stage: 'SCRIPT_REVIEW' },
  { id: 'gate_character_confirmation', stage: 'ASSET_REVIEW' },
  { id: 'gate_shot_confirmation', stage: 'SHOT_REVIEW' },
  { id: 'gate_video_cost_confirmation', stage: 'VIDEO_REVIEW' },
  { id: 'gate_video_confirmation', stage: 'VIDEO_REVIEW' },
  { id: 'gate_export_confirmation', stage: 'PREVIEW_READY' },
]

const activeStage = computed(() => (route.meta.stage as string | undefined) ?? 'story')

/**
 * 当前可推断的阶段序号。
 * BLOCKED 用 `stage_before_block` 推算；STALE 时后端不再保留原阶段，故返回 null
 * （此时不渲染「已完成」勾选，改为在门控区提示受影响资源）。
 */
const progressIndex = computed<number | null>(() => {
  const stage = episode.value?.stage
  if (!stage) return null
  if (stage === 'STALE') return null
  if (stage === 'BLOCKED') {
    const before = episode.value?.stage_before_block
    return before ? stageIndex(before) : null
  }
  return stageIndex(stage)
})

/** 阻断态落在哪个工作台阶段（用于左侧栏红色高亮）。 */
const blockedStageKey = computed<string | null>(() => {
  if (episode.value?.stage !== 'BLOCKED') return null
  const before = episode.value?.stage_before_block
  return (before ? STAGE_ROUTE[before] : STAGE_ROUTE.BLOCKED) ?? null
})

/** §10.1：STALE 时受影响的是画面资源（后端 STAGE_ROUTE.STALE = shots）。 */
const staleStageKey = computed<string | null>(() =>
  episode.value?.stage === 'STALE' ? STAGE_ROUTE.STALE : null,
)

/** 待确认门控落在哪个工作台阶段。 */
const pendingGateStageKey = computed<string | null>(() =>
  pendingGate.value ? (GATE_ROUTE[pendingGate.value] ?? null) : null,
)

function stageDone(stage: StageDef): boolean {
  const idx = progressIndex.value
  if (idx === null) return false
  return idx > stageIndex(stage.stage)
}

const doneStageCount = computed(() => STAGES.filter((s) => stageDone(s)).length)
const stageProgressLabel = computed(() =>
  progressIndex.value === null ? '—' : `${doneStageCount.value}/${STAGES.length}`,
)

const sseLabel = computed(() => {
  if (sseDegraded.value) return '实时已降级 · 轮询兜底'
  switch (sseState.value) {
    case 'live':
      return '实时连接中'
    case 'connecting':
      return '连接中…'
    case 'reconnecting':
      return '重连中…'
    case 'error':
      return '连接异常'
    case 'closed':
      return '未连接'
    default:
      return '空闲'
  }
})

const sseTone = computed(() => {
  if (sseDegraded.value) return 'warn'
  if (sseState.value === 'live') return 'ok'
  if (sseState.value === 'error') return 'danger'
  return 'muted'
})

const stageBadgeTone = computed(() => {
  const s = episode.value?.stage
  if (s === 'BLOCKED') return 'danger'
  // §10.1：STALE 不是错误，是「有内容待更新」的提醒
  if (s === 'STALE') return 'warn'
  if (s === 'EXPORTED') return 'ok'
  if (s === 'PREVIEW_READY') return 'ok'
  return 'info'
})

/** §10.1：受影响资源提示（后端 stale_shot_count / stale_reasons）。 */
const staleShots = computed(() => Number(episode.value?.stale_shot_count ?? 0))
const staleReasons = computed<string[]>(() => {
  const raw = episode.value?.stale_reasons
  return Array.isArray(raw) ? raw.map(String) : []
})

const staleFlags = computed(() => {
  const raw = episode.value?.stale_flags
  if (!raw) return []
  if (Array.isArray(raw)) return raw.map(String)
  return Object.entries(raw as Record<string, unknown>)
    .filter(([, v]) => Boolean(v))
    .map(([k]) => k)
})

/** 累计成本：后端 episode.total_cost_cents（分）→ 元，保留两位小数。 */
const totalCostCents = computed<number | null>(() => {
  const raw = episode.value?.total_cost_cents
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
})
const totalCostLabel = computed(() => formatCents(totalCostCents.value))

/** 保存状态：由 api 层写操作广播驱动（真实反映保存/提交/门控决策）。 */
const saveLabel = computed(() => {
  if (saving.value) return '保存中…'
  if (saveError.value) return '保存失败'
  if (lastSavedAt.value) return `已保存 · ${clockLabel(lastSavedAt.value)}`
  return '已保存'
})
const saveTone = computed(() => {
  if (saving.value) return 'info'
  if (saveError.value) return 'danger'
  return 'muted'
})
const saveTitle = computed(() => {
  if (saving.value) return '本页写操作进行中（保存内容 / 提交任务 / 门控决策）'
  if (saveError.value) return `最近一次写操作失败：${saveError.value}`
  if (lastSavedAt.value) return `最近一次写操作成功于 ${new Date(lastSavedAt.value).toLocaleString('zh-CN')}`
  return '本次会话尚无写操作，内容保存在后端'
})

function clockLabel(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

const durationRatio = computed(() => {
  const max = config.episodeMaxMs
  if (!max || !totalDurationMs.value) return 0
  return Math.min(100, (totalDurationMs.value / max) * 100)
})

/* ------------------------------------------------------------------ *
 * 3. 右侧：智能体建议、错误提示、版本和操作日志
 * ------------------------------------------------------------------ */

const logsLoading = ref(false)

/** 节点英文名 → 中文名（来自 /workflow/graph）。 */
const nodeLabels = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const node of graph.value) {
    const key = String(node.node ?? node.id ?? '')
    if (key) map[key] = String(node.label ?? key)
  }
  return map
})

function nodeLabel(name?: string | null): string {
  if (!name) return '—'
  return nodeLabels.value[name] ?? name
}

/** 操作日志按时间倒序（后端已倒序，这里再兜一次，避免依赖顺序）。 */
const sortedLogs = computed<WorkflowLogEntry[]>(() =>
  [...logs.value].sort(
    (a, b) => (Date.parse(b.created_at ?? '') || 0) - (Date.parse(a.created_at ?? '') || 0),
  ),
)

/**
 * 智能体建议：后端没有独立的「建议」接口，因此取当前节点最新的一条节点日志消息
 * （`GET /workflow/logs` 的真实 message）。没有日志时渲染空态，不编造内容。
 */
const suggestions = computed<WorkflowLogEntry[]>(() => {
  const list = sortedLogs.value
  if (!list.length) return []
  const current = workflow.value?.current_node ?? null
  if (current) {
    const own = list.filter((entry) => entry.node === current)
    if (own.length) return own.slice(0, 2)
  }
  return list.slice(0, 1)
})

const suggestionNodeLabel = computed(() => {
  const first = suggestions.value[0]
  if (!first) return null
  return nodeLabel(first.node)
})

const workflowVersion = computed<number | null>(() => {
  const v = workflow.value?.state?.version
  return typeof v === 'number' ? v : null
})

const currentError = computed(() => error.value ?? workflowError.value)

function logStatus(entry: WorkflowLogEntry): string {
  const raw = entry.status ?? entry.level
  return typeof raw === 'string' && raw ? raw : '—'
}

function logTone(entry: WorkflowLogEntry): string {
  const s = logStatus(entry).toUpperCase()
  if (s.includes('FAIL') || s.includes('ERROR')) return 'badge-danger'
  if (s.includes('WAIT')) return 'badge-warn'
  if (s.includes('RUN') || s.includes('START')) return 'badge-info'
  if (s.includes('SUCCEED') || s.includes('DONE') || s.includes('OK') || s.includes('COMPLET')) {
    return 'badge-ok'
  }
  return 'badge-muted'
}

function logDuration(entry: WorkflowLogEntry): string {
  const raw = entry.duration_ms
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return ''
  return `${(raw / 1000).toFixed(1)}s`
}

async function reloadLogs(): Promise<void> {
  logsLoading.value = true
  try {
    await store.loadLogs()
  } finally {
    logsLoading.value = false
  }
}

/* ------------------------------------------------------------------ *
 * 3.5 FR-004 版本记录
 * ------------------------------------------------------------------ */

const versionRecords = ref<VersionRecord[]>([])
const versionsLoading = ref(false)
const versionDetail = ref<(VersionRecord & { diff: VersionDiff }) | null>(null)
const versionDetailLoading = ref<string | null>(null)

async function reloadVersions(): Promise<void> {
  const id = props.episodeId
  if (!id) return
  versionsLoading.value = true
  try {
    const data = await versionsApi.list(id, { limit: 100 })
    versionRecords.value = data.items ?? []
    // 列表刷新后原详情可能已不在其中，收起以免展示陈旧数据
    if (versionDetail.value && !versionRecords.value.some((r) => r.id === versionDetail.value?.id)) {
      versionDetail.value = null
    }
  } catch {
    // 版本记录是辅助信息，取不到时保持空态，不打断主流程
    versionRecords.value = []
  } finally {
    versionsLoading.value = false
  }
}

async function toggleVersionDetail(record: VersionRecord): Promise<void> {
  if (versionDetail.value?.id === record.id) {
    versionDetail.value = null
    return
  }
  versionDetailLoading.value = record.id
  try {
    versionDetail.value = await versionsApi.detail(props.episodeId, record.id)
  } catch {
    versionDetail.value = null
  } finally {
    versionDetailLoading.value = null
  }
}

/** 差异值可能是长文本或对象，截断成一行便于扫读。 */
function shortValue(value: unknown): string {
  if (value === null || value === undefined) return '（空）'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 60 ? `${text.slice(0, 60)}…` : text
}

/* ------------------------------------------------------------------ *
 * 4. 底部：任务进度、批量操作、下一步
 * ------------------------------------------------------------------ */

/** 本集任务：SSE 实时快照优先，叠加 `GET /jobs?episode_id=` 的近期任务。 */
const recentJobs = computed<Job[]>(() => {
  // 只取 store 明确跟踪过的任务：SSE 事件可能带来字段不全的临时快照，不能直接展示。
  const live = watchedJobIds.value
    .map((id) => jobSnapshots.value[id])
    .filter((job) => Boolean(job) && (!job.episode_id || job.episode_id === props.episodeId))
  const liveIds = new Set(live.map((job) => job.id))
  const merged = [...live, ...episodeJobs.value.filter((job) => !liveIds.has(job.id))]
  return merged
    .sort((a, b) => {
      const ta = isTerminalJobStatus(a.status) ? 1 : 0
      const tb = isTerminalJobStatus(b.status) ? 1 : 0
      if (ta !== tb) return ta - tb
      return (Date.parse(b.created_at ?? '') || 0) - (Date.parse(a.created_at ?? '') || 0)
    })
    .slice(0, 8)
})

const runningJobs = computed(() => recentJobs.value.filter((job) => !isTerminalJobStatus(job.status)))

/** 失败任务（右侧「错误提示」补充真实失败原因）。 */
const failedJobs = computed(() =>
  recentJobs.value.filter((job) => job.status === 'FAILED' || job.status === 'EXPIRED'),
)

function jobProgress(job: Job): number {
  return Math.max(0, Math.min(100, job.progress ?? 0))
}

function jobTone(job: Job): string {
  if (job.status === 'FAILED' || job.status === 'EXPIRED') return 'danger'
  if (isTerminalJobStatus(job.status)) return 'ok'
  return ''
}

/** 后台补拉回来的任务：状态还是 FAILED，但产物已经拿到了。 */
function jobRecovered(job: Job): boolean {
  return Boolean((job.result_ref as Record<string, unknown> | null)?.recovered)
}

/**
 * 任务卡上的费用说明。
 *
 * 「失败了还扣钱」是最容易让人起疑的地方，所以这里直接把结果写出来：
 * 失败/取消的任务预留已全额退回，一个词都不用猜；成功的才显示实际花费。
 */
function jobCostNote(job: Job): string {
  if (!isTerminalJobStatus(job.status)) return ''
  if (job.status === 'SUCCEEDED') {
    const cents = job.cost_settled_cents ?? 0
    return cents > 0 ? `花费 ${formatCents(cents)}` : ''
  }
  if (job.status === 'FAILED' || job.status === 'EXPIRED' || job.status === 'CANCELLED') {
    // 后台已经补拉回来了：钱确实花了，别再显示「未扣费」
    if (jobRecovered(job)) {
      const cents = job.cost_settled_cents ?? 0
      return cents > 0 ? `已自动补回 · 花费 ${formatCents(cents)}` : '已自动补回'
    }
    return '未扣费 · 预留已退回'
  }
  return ''
}

function trackJob(job: Job | null | undefined): void {
  if (!job?.id) return
  store.trackJob(job)
  if (!episodeJobs.value.some((item) => item.id === job.id)) {
    episodeJobs.value = [job, ...episodeJobs.value]
  }
}

/** 底栏任务条上的「停止」：排队中立即取消，执行中在当前这一张/一条之后停。 */
async function stopJob(job: Job): Promise<void> {
  try {
    const updated = await cancelJobRequest(job.id)
    if (updated) trackJob(updated)
  } catch {
    /* api 层已提示 */
  }
}

const batchBusy = ref(false)

async function runBatch(fn: () => Promise<void>): Promise<void> {
  if (batchBusy.value) return
  batchBusy.value = true
  try {
    await fn()
  } catch {
    /* 错误已由 api 层 toast 展示 */
  } finally {
    batchBusy.value = false
  }
}

const unconfirmedShotNos = computed(() =>
  shots.value.filter((shot) => !shot.confirmed_media_id).map((shot) => shot.shot_no),
)
const videoPendingShotNos = computed(() =>
  shots.value.filter((shot) => shot.confirmed_media_id && !shot.video_media_id).map((s) => s.shot_no),
)

/**
 * 分镜阶段：批量生成画面（复用 `POST /episodes/{id}/shots/generate-images`）。
 *
 * 只生成**用户勾选**的镜头：勾选状态存在 episode store 里，与分镜页表格共享。
 * 之前这里取的是「全部未确认镜头」，于是用户只勾了 1 个，一点却生成 6 个。
 */
/** 生成前的花费确认：先看张数×单价，避免一按就扣分。 */
const imageEstimate = ref<ImageEstimate | null>(null)
const imageEstimateOpen = ref(false)

async function generateAllImages(): Promise<void> {
  const nos = store.selectedShotNos
  if (!nos.length) {
    toast.warn('请先勾选镜头', '在分镜表里勾选要生成的镜头，再点这里')
    return
  }
  const info = await shotsApi.estimateImages(props.episodeId, {
    shot_nos: nos,
    candidates_per_shot: config.candidateDefault,
  })
  if (info.can_generate === false) {
    const detail = (info.blocking_problems ?? []).map((p) => `#${p.shot_no} ${p.message}`).join('；')
    toast.error('有镜头还不能生成', detail || '请先补齐角色参考图')
    return
  }
  imageEstimate.value = info
  imageEstimateOpen.value = true
}

async function confirmGenerateImages(): Promise<void> {
  const nos = store.selectedShotNos
  if (!nos.length) return
  imageEstimateOpen.value = false
  const job = await shotsApi.generateImages(props.episodeId, {
    shot_nos: nos,
    candidates_per_shot: config.candidateDefault,
  })
  trackJob(job)
  toast.info('分镜图生成任务已提交', `${nos.length} 个镜头 × ${config.candidateDefault} 张候选`)
}

/** 分镜阶段：批量确认画面（复用 `POST /episodes/{id}/shots/confirm-batch`）。 */
async function confirmAllShots(): Promise<void> {
  const nos = unconfirmedShotNos.value
  if (!nos.length) {
    toast.info('没有待确认的镜头')
    return
  }
  const result = (await shotsApi.confirmBatch(props.episodeId, nos)) as {
    confirmed?: number[]
    items?: { ok?: boolean }[]
  }
  const ok = result.confirmed?.length ?? result.items?.filter((item) => item.ok).length ?? nos.length
  toast.success('批量确认已提交', `确认 ${ok} 个镜头`)
  await Promise.all([store.loadShots(), store.refreshLive()])
}

/** 视频阶段：先估算成本（PRD 要求展示成本后再生成）。 */
const videoQuote = ref<VideoQuote | null>(null)

async function estimateVideo(): Promise<void> {
  const nos = videoPendingShotNos.value
  if (!nos.length) {
    toast.info('没有待生成视频的镜头', '需要先确认画面')
    return
  }
  videoQuote.value = await videoApi.estimate(props.episodeId, { shot_nos: nos })
  const quota =
    videoQuote.value.quota_after_cents === undefined
      ? ''
      : `，估算后剩余 ${formatCents(videoQuote.value.quota_after_cents)}`
  toast.info('视频成本估算完成', `预估 ${formatCents(videoQuote.value.estimate_cents)}${quota}`)
}

/** 视频阶段：确认成本后提交生成（复用 `POST /episodes/{id}/video/generate`）。 */
async function generateVideo(): Promise<void> {
  const quote = videoQuote.value
  if (!quote) return
  const nos = videoPendingShotNos.value
  if (!nos.length) {
    toast.info('没有待生成视频的镜头')
    return
  }
  const job = await videoApi.generate(props.episodeId, { shot_nos: nos, quote_id: quote.quote_id })
  trackJob(job)
  videoQuote.value = null
  toast.info('视频生成任务已提交', `${nos.length} 个镜头`)
}

/** 故事阶段：生成剧本（复用 `POST /episodes/{id}/story/generate`）。 */
async function generateScript(): Promise<void> {
  const job = await storyApi.generate(props.episodeId)
  trackJob(job)
  toast.info('剧本生成任务已提交')
}

/** 故事阶段：故事分析（复用 `POST /episodes/{id}/story/analyze`）。 */
async function analyzeStory(): Promise<void> {
  const result = await storyApi.analyze(props.episodeId)
  const theme = result.analysis?.theme ? `主题：${result.analysis.theme}` : '已完成结构分析'
  toast.success('故事分析完成', theme)
}

/** 音频阶段：批量生成配音。 */
async function generateTts(): Promise<void> {
  const job = await audioApi.ttsGenerate(props.episodeId, {})
  trackJob(job)
  toast.info('配音生成任务已提交')
}

/** 音频阶段：生成字幕。 */
async function generateSubtitles(): Promise<void> {
  const job = await audioApi.subtitlesGenerate(props.episodeId)
  trackJob(job)
  toast.info('字幕生成任务已提交')
}

/** 合成阶段：低码率预览。 */
async function renderPreview(): Promise<void> {
  const job = await renderApi.preview(props.episodeId, { watermark: false })
  trackJob(job)
  toast.info('预览合成任务已提交')
}

/** 合成阶段：打包素材 ZIP。 */
async function exportPackage(): Promise<void> {
  const job = await renderApi.package(props.episodeId)
  trackJob(job)
  toast.info('素材包打包任务已提交')
}

interface StageAction {
  key: string
  label: string
  title: string
  primary?: boolean
  disabled?: boolean
  run: () => void
}

/** 批量操作：只放真实存在、且当前阶段可用的接口动作。 */
const stageActions = computed<StageAction[]>(() => {
  switch (activeStage.value) {
    case 'story':
      return [
        {
          key: 'gen-script',
          label: '生成剧本',
          title: '调用 /story/generate 生成故事与分镜初稿',
          primary: true,
          run: () => void runBatch(generateScript),
        },
        {
          key: 'analyze',
          label: '分析故事',
          title: '调用 /story/analyze 输出人物、场景与冲突结构',
          run: () => void runBatch(analyzeStory),
        },
      ]
    case 'shots':
      return [
        {
          key: 'gen-images',
          label: `生成画面${store.selectedShotNos.length ? `（${store.selectedShotNos.length}）` : ''}`,
          title: '为**已勾选**的镜头生成候选图（先在分镜表里勾选）',
          primary: true,
          run: () => void runBatch(generateAllImages),
        },
        {
          key: 'confirm-all',
          label: '确认全部画面',
          title: '批量确认未确认镜头的主图（/shots/confirm-batch）',
          disabled: !unconfirmedShotNos.value.length,
          run: () => void runBatch(confirmAllShots),
        },
      ]
    case 'video':
      return [
        {
          key: 'estimate',
          label: '估算视频成本',
          title: '调用 /video/estimate 取得报价与配额余量',
          disabled: !videoPendingShotNos.value.length,
          run: () => void runBatch(estimateVideo),
        },
        {
          key: 'gen-video',
          label: '确认并生成视频',
          title: '按报价确认成本后提交视频生成',
          primary: true,
          disabled: !videoQuote.value || !videoPendingShotNos.value.length,
          run: () => void runBatch(generateVideo),
        },
      ]
    case 'audio':
      return [
        {
          key: 'tts',
          label: '生成配音',
          title: '调用 /tts/generate 批量生成对白',
          primary: true,
          run: () => void runBatch(generateTts),
        },
        {
          key: 'subtitles',
          label: '生成字幕',
          title: '调用 /subtitles/generate 生成字幕',
          run: () => void runBatch(generateSubtitles),
        },
      ]
    case 'render':
      return [
        {
          key: 'preview',
          label: '生成预览',
          title: '调用 /render/preview 合成低码率预览',
          primary: true,
          run: () => void runBatch(renderPreview),
        },
        {
          key: 'package',
          label: '打包素材',
          title: '调用 /export/package 打包 ZIP 素材包',
          run: () => void runBatch(exportPackage),
        },
      ]
    default:
      // 角色与画风阶段：资产生成必须针对具体角色（/characters/{id}/generate），
      // 布局层拿不到「当前角色」，因此不提供批量入口，避免误操作。
      return []
  }
})

/** 下一步：按 STAGES 顺序推进，门控待确认时禁用。 */
const activeIndex = computed(() => STAGES.findIndex((s) => s.key === activeStage.value))
const nextStage = computed(() =>
  activeIndex.value >= 0 ? (STAGES[activeIndex.value + 1] ?? null) : null,
)
const nextRoute = computed(() => (nextStage.value ? STAGE_ROUTE[nextStage.value.stage] : null))
const nextDisabled = computed(
  () => Boolean(pendingGate.value) || !nextStage.value || !nextRoute.value || !episode.value,
)
const nextLabel = computed(() =>
  nextStage.value ? `下一步：${nextStage.value.label}` : '已是最后阶段',
)
const nextHint = computed(() => {
  if (pendingGate.value) return `待确认门控：${gateLabel(pendingGate.value)}，确认后才能推进`
  if (!nextStage.value) return '导出完成后可在预览页下载成品'
  return `当前阶段：${STAGES[activeIndex.value]?.label ?? '—'}`
})

function goNext(): void {
  const name = nextRoute.value ? `episode-${nextRoute.value}` : null
  if (!name) return
  void router.push({ name, params: { episodeId: props.episodeId } })
}

/* ------------------------------------------------------------------ *
 * 5. 数据加载与跳转
 * ------------------------------------------------------------------ */

async function reload(): Promise<void> {
  await store.loadAll(props.episodeId)
  await store.loadLogs()
  store.startLive()
}

onMounted(() => {
  void reload()
  void reloadVersions()
  if (typeof window.matchMedia === 'function') {
    mediaQuery = window.matchMedia('(max-width: 1180px)')
    narrow.value = mediaQuery.matches
    sideCollapsed.value = mediaQuery.matches
    mediaQuery.addEventListener('change', onMediaChange)

    // 右栏是「建议 / 错误 / 版本日志」这类参考信息：窄于 1560 时先收起来，
    // 把宽度让给真正在编辑的分镜与画面。
    wideQuery = window.matchMedia('(min-width: 1560px)')
    railCollapsed.value = mediaQuery.matches || !wideQuery.matches
    wideQuery.addEventListener('change', onWideChange)
  }
  if (topRef.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      topHeight.value = topRef.value?.offsetHeight ?? 64
    })
    resizeObserver.observe(topRef.value)
  }
  // 底栏是 sticky 的，会盖住最后一段内容；这里把真实高度同步给内容区留白
  if (bottomRef.value && typeof ResizeObserver !== 'undefined') {
    bottomObserver = new ResizeObserver(() => {
      bottomHeight.value = bottomRef.value?.offsetHeight ?? 112
    })
    bottomObserver.observe(bottomRef.value)
  }
})

watch(
  () => props.episodeId,
  (next, prev) => {
    if (next === prev) return
    store.reset()
    videoQuote.value = null
    void reload()
  },
)

/** 节点切换时补一次日志，让右侧「版本和操作日志」跟上工作流推进。 */
watch(
  () => workflow.value?.current_node,
  (next, prev) => {
    if (next === prev) return
    void store.loadLogs()
  },
)

onBeforeUnmount(() => {
  store.stopLive()
  mediaQuery?.removeEventListener('change', onMediaChange)
  wideQuery?.removeEventListener('change', onWideChange)
  resizeObserver?.disconnect()
  bottomObserver?.disconnect()
})

function openGate(): void {
  const gate = pendingGate.value
  if (!gate) return
  const target = GATE_ROUTE[gate] ?? 'story'
  const name = `episode-${target}`
  void router.push({ name, params: { episodeId: props.episodeId } })
}

async function submitCopy(): Promise<void> {
  copying.value = true
  try {
    const created = await episodesApi.copy(props.episodeId, copyTitle.value.trim() || undefined)
    toast.success('剧集已复制', `新剧集：${created.title}`)
    showCopy.value = false
    copyTitle.value = ''
    if (created.id) {
      void router.push({ name: 'episode-story', params: { episodeId: created.id } })
    }
  } catch (err) {
    void err
  } finally {
    copying.value = false
  }
}
</script>

<template>
  <div
    class="workbench"
    :class="{ 'side-collapsed': sideCollapsed, 'rail-collapsed': railCollapsed, narrow }"
    :style="{ '--wb-top-h': `${topHeight}px`, '--wb-bottom-h': `${bottomHeight}px` }"
  >
    <!-- ============ 顶部区：第一行 = 身份与动作，第二行 = 元信息与指标 ============ -->
    <header ref="topRef" class="wb-top">
      <div class="wb-top-row">
        <div class="wb-top-left">
          <button
            type="button"
            class="btn btn-sm btn-ghost wb-toggle"
            :aria-pressed="!sideCollapsed"
            title="显示或隐藏左侧阶段导航"
            @click="sideCollapsed = !sideCollapsed"
          >
            ☰<span class="wb-toggle-text"> 阶段</span>
          </button>
          <RouterLink
            v-if="episode?.project_id"
            :to="{ name: 'project-detail', params: { projectId: episode?.project_id } }"
            class="wb-back"
            title="返回项目"
          >
            ←
          </RouterLink>
          <div class="wb-title-wrap">
            <div class="wb-title-row">
              <h1 class="title-lg ellipsis">
                {{ episode?.title ?? '加载中…' }}
              </h1>
              <span v-if="episode?.episode_no" class="badge mono">
                EP{{ String(episode.episode_no).padStart(2, '0') }}
              </span>
              <span class="badge" :class="`badge-${stageBadgeTone}`">{{ stageLabel(episode?.stage) }}</span>
              <span v-if="pendingGate" class="badge badge-warn badge-dot">{{ gateLabel(pendingGate) }}</span>
              <span v-if="staleFlags.length" class="badge badge-warn" :title="staleFlags.join('、')">
                {{ staleFlags.length }} 项下游过期
              </span>
              <!-- §10.1：受影响资源只标记不删除，给出重新生成入口 -->
              <RouterLink
                v-if="staleShots"
                class="badge badge-warn badge-link"
                :to="`/episodes/${episode?.id}/shots`"
                :title="staleReasons.join('、')"
              >
                {{ staleShots }} 镜待更新
              </RouterLink>
            </div>
            <div v-if="episode?.blocked_reason" class="wb-blocked small">
              已阻断：{{ episode.blocked_reason }}
              <span v-if="episode?.stage_before_block" class="dim">
                （解除后回到 {{ stageLabel(episode.stage_before_block) }}）
              </span>
            </div>
          </div>
        </div>

        <div class="wb-top-actions">
          <button
            v-if="!workflow"
            type="button"
            class="btn btn-sm btn-primary"
            :disabled="starting || loading"
            @click="startWorkflow"
          >
            {{ starting ? '启动中…' : '启动工作流' }}
          </button>
          <button
            v-if="pendingGate"
            type="button"
            class="btn btn-sm btn-primary"
            @click="openGate"
          >
            去确认
          </button>
          <button type="button" class="btn btn-sm btn-ghost" @click="showCopy = true">复制剧集</button>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="store.refreshing"
            @click="store.refreshAll()"
          >
            刷新
          </button>
          <button
            type="button"
            class="btn btn-sm btn-ghost wb-toggle"
            :aria-pressed="!railCollapsed"
            title="显示或隐藏右侧智能体面板"
            @click="toggleRail"
          >
            <span class="wb-toggle-text">建议</span> ☷
          </button>
        </div>
      </div>

      <div class="wb-top-meta">
        <div class="wb-sub tiny mono dim">
          <span class="wb-id" :title="episode?.id">剧集 {{ episode?.episode_no ?? '—' }}</span>
          <span v-if="episode?.target_duration_ms">
            目标 {{ (episode.target_duration_ms / 1000).toFixed(0) }}s
          </span>
          <span v-if="shotCount">分镜 {{ confirmedShotCount }}/{{ shotCount }}</span>
          <span v-if="totalDurationMs">时长 {{ (totalDurationMs / 1000).toFixed(1) }}s</span>
        </div>

        <!-- 总进度：本集时长 / 上限 -->
        <div v-if="durationRatio > 0" class="wb-duration">
          <span class="tiny dim nowrap">进度</span>
          <div class="progress">
            <div
              class="progress-bar"
              :class="{ warn: durationRatio > 92 }"
              :style="{ width: `${durationRatio}%` }"
            />
          </div>
          <span class="tiny dim-2 mono nowrap">
            {{ durationRatio.toFixed(0) }}% · {{ (totalDurationMs / 1000).toFixed(1) }}s /
            {{ (config.episodeMaxMs / 1000).toFixed(0) }}s 上限
          </span>
        </div>

        <div class="wb-top-right">
          <!-- 阶段进度 / 累计成本 / 保存状态（PRD §5.2 顶部五项） -->
          <div class="wb-stats">
            <div class="wb-stat" title="已推进的工作台阶段数 / 总阶段数">
              <span class="wb-stat-label">阶段</span>
              <span class="wb-stat-value num">{{ stageProgressLabel }}</span>
            </div>
            <div class="wb-stat" title="本剧集累计成本（分 → 元）">
              <span class="wb-stat-label">累计成本</span>
              <span class="wb-stat-value num">{{ totalCostLabel }}</span>
            </div>
            <div class="wb-stat" :title="saveTitle">
              <span class="wb-stat-label">保存</span>
              <span class="badge badge-dot" :class="`badge-${saveTone}`">{{ saveLabel }}</span>
            </div>
          </div>

          <span class="badge badge-dot" :class="`badge-${sseTone}`" :title="`事件流状态：${sseLabel}`">
            {{ sseLabel }}
          </span>
        </div>
      </div>
    </header>

    <!-- ============ 中部区（含左侧栏与右侧栏） ============ -->
    <div class="wb-main">
      <!-- ---- 左侧区：生产阶段导航 + 门控状态 ---- -->
      <aside class="wb-left" aria-label="生产阶段导航">
        <div class="wb-rail-inner">
          <div class="wb-rail-head">
            <span class="section-title">生产阶段</span>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              title="收起左侧栏"
              @click="sideCollapsed = true"
            >
              «
            </button>
          </div>

          <nav class="wb-stages" aria-label="制作阶段">
            <RouterLink
              v-for="stage in STAGES"
              :key="stage.key"
              :to="{ name: stage.route, params: { episodeId } }"
              class="stage-tab"
              :class="{
                active: activeStage === stage.key,
                done: stageDone(stage),
                blocked: blockedStageKey === stage.key,
                stale: staleStageKey === stage.key,
              }"
            >
              <span class="stage-no mono" aria-hidden="true">
                {{ String(STAGES.indexOf(stage) + 1).padStart(2, '0') }}
              </span>
              <span class="stage-label ellipsis">{{ stage.label }}</span>
              <span v-if="stageDone(stage)" class="stage-tick" title="该阶段已推进">✓</span>
              <span
                v-else-if="pendingGateStageKey === stage.key"
                class="stage-gate"
                title="该阶段有待确认门控"
                >◆</span
              >
              <span v-else-if="blockedStageKey === stage.key" class="stage-gate" title="已阻断">✕</span>
              <span v-else-if="staleStageKey === stage.key" class="stage-gate" title="有内容待更新">⟳</span>
            </RouterLink>
          </nav>

          <hr class="divider" />

          <!-- 门控状态：待确认高亮 + 已通过勾选 -->
          <section class="wb-gates">
            <div class="row-between">
              <span class="section-title">门控状态</span>
              <span class="badge badge-dot" :class="pendingGate ? 'badge-warn' : 'badge-muted'">
                {{ pendingGate ? gateLabel(pendingGate) : '无待确认' }}
              </span>
            </div>
            <ul class="gate-list">
              <li
                v-for="gate in GATE_LIST"
                :key="gate.id"
                class="gate-item"
                :class="{
                  pending: pendingGate === gate.id,
                  passed:
                    progressIndex !== null && progressIndex > stageIndex(gate.stage),
                }"
                :title="GATE_LABEL[gate.id]"
              >
                <span class="gate-mark" aria-hidden="true">
                  {{
                    pendingGate === gate.id
                      ? '◆'
                      : progressIndex !== null && progressIndex > stageIndex(gate.stage)
                        ? '✓'
                        : '○'
                  }}
                </span>
                <span class="gate-name ellipsis">{{ GATE_LABEL[gate.id] }}</span>
              </li>
            </ul>
            <p v-if="episode?.stage === 'BLOCKED'" class="tiny wb-rail-note danger">
              已阻断：{{ episode.blocked_reason || '原因未提供' }}
            </p>
            <p v-else-if="episode?.stage === 'STALE'" class="tiny wb-rail-note warn">
              {{ staleShots || '部分' }} 个镜头待更新，确认后会推进工作流。
            </p>
          </section>

          <hr class="divider" />

          <!-- 工作流节点进度（纵向紧凑变体） -->
          <NodeProgress
            vertical
            :nodes="graph"
            :current="workflow?.current_node ?? null"
            :pending-gate="pendingGate"
          >
            <template #empty-action>
              <button
                type="button"
                class="btn btn-sm btn-primary"
                :disabled="loading"
                @click="store.startWorkflow()"
              >
                启动工作流
              </button>
            </template>
          </NodeProgress>
        </div>
      </aside>

      <!-- ---- 中部区：当前阶段主工作区 ---- -->
      <main class="wb-center">
        <ErrorState v-if="error && !episode" :error="error" context="加载剧集" @retry="reload" />
        <SkeletonBlock v-else-if="loading && !episode" variant="cards" :rows="3" />
        <RouterView v-else />
      </main>

      <!-- ---- 右侧区：智能体建议 / 错误提示 / 版本和操作日志 ---- -->
      <aside class="wb-right" aria-label="智能体面板">
        <div class="wb-rail-inner">
          <div class="wb-rail-head">
            <span class="section-title">智能体面板</span>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              title="收起右侧栏"
              @click="collapseRail"
            >
              »
            </button>
          </div>

          <!-- 智能体建议 -->
          <section class="card">
            <div class="card-head">
              <div class="row">
                <h2 class="title-md">智能体建议</h2>
                <span v-if="workflow?.current_node" class="badge badge-info badge-dot">
                  {{ nodeLabel(workflow.current_node) }}
                </span>
              </div>
            </div>
            <div class="card-body col">
              <EmptyState
                v-if="!suggestions.length"
                compact
                icon="✧"
                title="暂无智能体建议"
                description="后端未提供独立的建议接口；工作流产生节点日志后，这里会显示当前节点最新的一条消息。"
              />
              <template v-else>
                <article v-for="(item, i) in suggestions" :key="i" class="suggest-item">
                  <div class="row-wrap tiny dim">
                    <span class="badge" :class="logTone(item)">{{ logStatus(item) }}</span>
                    <span class="mono">{{ nodeLabel(item.node) }}</span>
                    <span class="dim-2">{{ formatDateTime(item.created_at) }}</span>
                  </div>
                  <p class="small pre-wrap">{{ item.message || '（该节点未写入消息）' }}</p>
                </article>
                <p class="tiny dim-2">
                  来源：工作流节点日志
                  <template v-if="suggestionNodeLabel"> · 节点「{{ suggestionNodeLabel }}」</template>
                </p>
              </template>
            </div>
          </section>

          <!-- 错误提示 -->
          <section class="card">
            <div class="card-head">
              <div class="row">
                <h2 class="title-md">错误提示</h2>
                <span v-if="failedJobs.length" class="badge badge-danger">
                  {{ failedJobs.length }} 个任务失败
                </span>
              </div>
            </div>
            <div class="card-body col">
              <ErrorState
                v-if="currentError"
                :error="currentError"
                context="剧集 / 工作流"
                compact
                @retry="reload"
              />
              <article v-for="job in failedJobs" :key="job.id" class="error-item">
                <div class="row-wrap tiny dim">
                  <JobBadge :status="job.status" />
                  <span class="mono">{{ jobTypeLabel(job.type) }}</span>
                  <span class="dim-2 mono">{{ job.id }}</span>
                </div>
                <p class="small pre-wrap">
                  <span class="mono">{{ job.error_code || 'ERROR' }}</span> ·
                  {{ job.error_message || '未返回错误信息' }}
                </p>
              </article>
              <EmptyState
                v-if="!currentError && !failedJobs.length"
                compact
                icon="✓"
                title="暂无错误"
                description="剧集、工作流与近期任务都没有返回错误。"
              />
            </div>
          </section>

          <!-- FR-004 版本记录：剧本/角色描述/分镜提示词/时间线 -->
          <section class="card">
            <div class="card-head">
              <div class="row">
                <h2 class="title-md">版本记录</h2>
                <span class="mono tiny dim">{{ versionRecords.length }} 条</span>
              </div>
              <button
                type="button"
                class="btn btn-sm btn-ghost"
                :disabled="versionsLoading"
                @click="reloadVersions"
              >
                {{ versionsLoading ? '加载中…' : '刷新' }}
              </button>
            </div>
            <div class="card-body col">
              <EmptyState
                v-if="!versionRecords.length"
                compact
                icon="⧉"
                title="暂无版本记录"
                description="保存剧本、修改角色描述或正式导出后，这里会留下可回溯的快照。"
              />
              <ul v-else class="version-list">
                <li v-for="record in versionRecords.slice(0, 40)" :key="record.id" class="version-item">
                  <div class="version-top">
                    <span class="badge badge-muted">{{ record.entity_label }}</span>
                    <span class="mono tiny">v{{ record.version }}</span>
                    <span class="mono tiny dim-2 nowrap">{{ formatDateTime(record.created_at) }}</span>
                  </div>
                  <p class="small dim ellipsis" :title="record.note">{{ record.note || '—' }}</p>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="versionDetailLoading === record.id"
                    @click="toggleVersionDetail(record)"
                  >
                    {{ versionDetail?.id === record.id ? '收起差异' : '查看差异' }}
                  </button>
                  <div v-if="versionDetail?.id === record.id" class="version-diff">
                    <EmptyState
                      v-if="!versionDetail.diff.changed_count"
                      compact
                      icon="="
                      title="与上一版一致"
                      description="内容指纹未变，未产生实际差异。"
                    />
                    <ul v-else>
                      <li v-for="f in versionDetail.diff.changed_fields.slice(0, 12)" :key="f.field">
                        <span class="mono tiny">{{ f.field }}</span>
                        <span class="diff-before">{{ shortValue(f.before) }}</span>
                        <span class="dim-2">→</span>
                        <span class="diff-after">{{ shortValue(f.after) }}</span>
                      </li>
                    </ul>
                  </div>
                </li>
              </ul>
            </div>
          </section>

          <!-- 版本和操作日志 -->
          <section class="card">
            <div class="card-head">
              <div class="row">
                <h2 class="title-md">版本和操作日志</h2>
                <span class="mono tiny dim">{{ logs.length }} 条</span>
                <span v-if="workflowVersion !== null" class="badge badge-muted mono">
                  version {{ workflowVersion }}
                </span>
              </div>
              <button
                type="button"
                class="btn btn-sm btn-ghost"
                :disabled="logsLoading"
                title="重新拉取 /workflow/logs"
                @click="reloadLogs"
              >
                {{ logsLoading ? '加载中…' : '刷新' }}
              </button>
            </div>
            <div class="card-body col">
              <EmptyState
                v-if="!sortedLogs.length"
                compact
                icon="≡"
                title="暂无操作日志"
                description="工作流启动后，后端会在 /workflow/logs 写入节点执行记录。"
              />
              <ul v-else class="log-list">
                <li v-for="(entry, i) in sortedLogs.slice(0, 50)" :key="`${entry.node}-${i}`" class="log-item">
                  <div class="log-top">
                    <span class="log-node ellipsis" :title="entry.node">{{ nodeLabel(entry.node) }}</span>
                    <span class="badge" :class="logTone(entry)">{{ logStatus(entry) }}</span>
                    <span v-if="logDuration(entry)" class="mono tiny dim-2">{{ logDuration(entry) }}</span>
                    <span class="mono tiny dim-2 nowrap">{{ formatDateTime(entry.created_at) }}</span>
                  </div>
                  <p class="log-msg small pre-wrap">{{ entry.message || '（无消息）' }}</p>
                </li>
              </ul>
            </div>
          </section>
        </div>
      </aside>
    </div>

    <!-- ============ 底部区：任务进度 / 批量操作 / 下一步 ============ -->
    <footer ref="bottomRef" class="wb-bottom">
      <section class="wb-bottom-col">
        <div class="row-between">
          <span class="section-title">任务进度</span>
          <span class="mono tiny dim">
            {{ runningJobs.length }} 进行中 / {{ recentJobs.length }} 近期
          </span>
        </div>
        <div v-if="recentJobs.length" class="job-strip">
          <div v-for="job in recentJobs" :key="job.id" class="job-chip">
            <div class="row-between">
              <JobBadge :status="job.status" />
              <span class="mono tiny dim-2">{{ jobProgress(job) }}%</span>
            </div>
            <div class="progress">
              <div
                class="progress-bar"
                :class="jobTone(job)"
                :style="{ width: `${jobProgress(job)}%` }"
              />
            </div>
            <div class="row-wrap job-chip-meta tiny dim">
              <span class="ellipsis">{{ jobTypeLabel(job.type) }}</span>
              <span class="mono">{{ formatDateTime(job.created_at) }}</span>
            </div>
            <span v-if="jobCostNote(job)" class="job-cost tiny">
              {{ jobCostNote(job) }}
            </span>
            <button
              v-if="!isTerminalJobStatus(job.status)"
              type="button"
              class="btn btn-sm btn-ghost job-chip-stop"
              :disabled="job.status === 'CANCELLING'"
              :title="
                job.status === 'CANCELLING'
                  ? '正在停止：当前这一张/一条跑完就结束'
                  : '已提交的这一张会跑完（照常计费），之后不再继续'
              "
              @click.stop="stopJob(job)"
            >
              {{ job.status === 'CANCELLING' ? '停止中…' : '停止' }}
            </button>
          </div>
        </div>
        <p v-else class="tiny dim">本剧集暂无任务记录。</p>
      </section>

      <section class="wb-bottom-col">
        <div class="row-between">
          <span class="section-title">批量操作</span>
          <span class="tiny dim">{{ STAGES[activeIndex]?.label ?? '当前阶段' }}</span>
        </div>
        <div v-if="stageActions.length" class="row-wrap">
          <button
            v-for="action in stageActions"
            :key="action.key"
            type="button"
            class="btn btn-sm"
            :class="action.primary ? 'btn-primary' : ''"
            :disabled="batchBusy || action.disabled"
            :title="action.title"
            @click="action.run()"
          >
            {{ action.label }}
          </button>
          <span v-if="videoQuote && activeStage === 'video'" class="badge badge-info mono">
            报价 {{ formatCents(videoQuote.estimate_cents) }}
          </span>
        </div>
        <p v-else class="tiny dim">
          当前阶段没有可批量执行的动作：角色资产生成需在「角色与画风」页逐个角色操作。
        </p>
      </section>

      <section class="wb-bottom-next">
        <button
          type="button"
          class="btn btn-primary"
          :disabled="nextDisabled"
          :title="nextHint"
          @click="goNext"
        >
          {{ nextLabel }}
        </button>
        <span class="tiny dim nowrap">{{ nextHint }}</span>
      </section>
    </footer>

    <ModalDialog
      :open="imageEstimateOpen"
      title="确认生成分镜图"
      :width="480"
      @close="imageEstimateOpen = false"
    >
      <template v-if="imageEstimate">
        <p class="small muted">
          将为 <b class="mono">{{ imageEstimate.shot_count }}</b> 个镜头各生成
          <b class="mono">{{ imageEstimate.candidates_per_shot }}</b> 张，共
          <b class="mono">{{ imageEstimate.image_count }}</b> 张图。
        </p>
        <p class="small">
          预计消耗 <b class="mono">{{ formatCents(imageEstimate.estimate_cents) }}</b>
          （单价 {{ formatCents(imageEstimate.unit_price_cents) }} / 张，
          {{ imageEstimate.provider }}）。
        </p>
        <p class="tiny dim">
          生成后余额 {{ formatCents(imageEstimate.quota_after_cents) }}
        </p>
      </template>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="imageEstimateOpen = false">取消</button>
        <button type="button" class="btn btn-primary" @click="confirmGenerateImages">
          确认生成
        </button>
      </template>
    </ModalDialog>

    <ModalDialog :open="showCopy" title="复制剧集" :width="460" @close="showCopy = false">
      <div class="field">
        <label class="label" for="copy-title">新剧集标题（留空则自动命名）</label>
        <input
          id="copy-title"
          v-model="copyTitle"
          class="input"
          placeholder="例如：第二话 · 雨夜追迹"
        />
        <span class="hint">复制剧本与资产引用，不复制进行中的任务。</span>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showCopy = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="copying" @click="submitCopy">
          {{ copying ? '复制中…' : '确认复制' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
/* ------------------------------------------------------------------ *
 * 五区外壳
 * ------------------------------------------------------------------ */

.workbench {
  --wb-left-w: 216px;
  --wb-right-w: 300px;
  display: flex;
  flex-direction: column;
  min-height: 100%;
}

.wb-main {
  flex: 1 1 auto;
  display: grid;
  grid-template-columns: var(--wb-left-w) minmax(0, 1fr) var(--wb-right-w);
  align-items: stretch;
  min-width: 0;
}

/* 折叠态：整列收起（不是压窄），中部拿到全部宽度 */
.workbench.side-collapsed .wb-left {
  display: none;
}
.workbench.rail-collapsed .wb-right {
  display: none;
}
.workbench.side-collapsed .wb-main {
  grid-template-columns: minmax(0, 1fr) var(--wb-right-w);
}
.workbench.rail-collapsed .wb-main {
  grid-template-columns: var(--wb-left-w) minmax(0, 1fr);
}
.workbench.side-collapsed.rail-collapsed .wb-main {
  grid-template-columns: minmax(0, 1fr);
}

.wb-left,
.wb-right {
  min-width: 0;
  background: rgba(243, 237, 227, 0.012);
}
.wb-left {
  border-right: 1px solid var(--line);
}
.wb-right {
  border-left: 1px solid var(--line);
}

/* 侧栏内部粘性滚动：顶部条以下保持可见 */
.wb-right .card-head {
  min-height: 40px;
  padding: var(--space-2) var(--space-3);
}
.wb-right .card-body {
  padding: var(--space-3);
}
.wb-right .card + .card {
  margin-top: 0;
}
/* 右栏很窄，空态不必铺满一大块：收紧内边距、去掉装饰图标 */
.wb-right .empty {
  padding: var(--space-3) var(--space-2);
  gap: var(--space-1);
  border-style: solid;
  background: transparent;
}
.wb-right .empty-icon {
  display: none;
}
.wb-right .empty-desc {
  font-size: var(--fs-11);
  max-width: none;
  line-height: 1.6;
}
/* 版本记录的「查看差异」不该占满整行 */
.wb-right .version-item .btn {
  align-self: flex-start;
  height: 24px;
  padding: 0 8px;
}
.wb-rail-inner {
  position: sticky;
  top: var(--wb-top-h, 60px);
  max-height: calc(100vh - var(--wb-top-h, 64px) - var(--wb-bottom-h, 112px));
  overflow: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.wb-rail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.wb-rail-note {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: rgba(243, 237, 227, 0.02);
}
.wb-rail-note.danger {
  border-color: rgba(224, 122, 106, 0.32);
  background: var(--danger-soft);
  color: var(--danger);
}
.wb-rail-note.warn {
  border-color: rgba(232, 203, 109, 0.32);
  background: var(--warn-soft);
  color: var(--warn);
}

.wb-center {
  container-type: inline-size;
  /* 底栏是 sticky 的，最后一段内容要能滚到它上面去 */
  padding: var(--space-5) var(--space-5) calc(var(--wb-bottom-h, 112px) + var(--space-4));
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  min-width: 0;
}

/* ------------------------------------------------------------------ *
 * 顶部区
 * ------------------------------------------------------------------ */

.wb-top {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-5) 6px;
  min-height: var(--topbar-h);
  background: rgba(18, 17, 16, 0.92);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--line);
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.4);
}

/* 第一行：身份（标题 + 状态徽标）与动作按钮，两端对齐 */
.wb-top-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-width: 0;
}
.wb-top-left {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}
.wb-back {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  color: var(--text-1);
  flex: 0 0 auto;
  transition:
    color var(--dur) var(--ease),
    border-color var(--dur) var(--ease),
    background var(--dur) var(--ease);
}
.wb-back:hover {
  color: var(--text-0);
  border-color: var(--line-strong);
  background: var(--neutral-soft);
}
.wb-title-wrap {
  min-width: 0;
}
.wb-title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  flex-wrap: wrap;
}
.wb-title-row h1 {
  max-width: 320px;
  margin-right: 2px;
}
.wb-blocked {
  margin-top: 3px;
  color: var(--danger);
}

/* 动作区：主按钮在前，弱操作在后，间距一致 */
.wb-top-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}

/* 第二行：等宽元信息 + 进度 + 指标，形成一条「仪表带」 */
.wb-top-meta {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-width: 0;
  padding-top: 6px;
  border-top: 1px dashed var(--line);
  flex-wrap: wrap;
}
.wb-sub {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  min-width: 0;
}
.wb-sub .wb-id {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}
/* 顶部区里的元信息用分隔点，避免一串数字糊在一起 */
.wb-sub > span + span::before {
  content: '·';
  margin-right: 12px;
  color: var(--text-3);
}

.wb-top-right {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  justify-content: flex-end;
  margin-left: auto;
}

/* 顶部三项关键指标：等宽数字，标签在上、值在下 */
.wb-stats {
  display: flex;
  align-items: stretch;
  gap: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: rgba(243, 237, 227, 0.02);
  overflow: hidden;
}
.wb-stat {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: var(--space-1) var(--space-3);
  min-width: 0;
  border-left: 1px solid var(--line);
}
.wb-stat:first-child {
  border-left: 0;
}
.wb-stat-label {
  font-size: var(--fs-11);
  letter-spacing: 0.06em;
  color: var(--text-3);
  line-height: 1.4;
}
.wb-stat-value {
  font-size: var(--fs-13);
  font-weight: 600;
  color: var(--text-0);
  white-space: nowrap;
  line-height: 1.4;
}
.wb-stat .badge {
  height: 18px;
  padding: 0 6px;
  font-size: var(--fs-11);
}
.wb-toggle {
  flex: 0 0 auto;
}

/* 进度：并入仪表带，不再单独占一行 */
.wb-duration {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1 1 220px;
  max-width: 360px;
  min-width: 160px;
}
.wb-duration .progress {
  flex: 1 1 auto;
}

/* ------------------------------------------------------------------ *
 * 左侧区：纵向阶段导航 + 门控状态
 * ------------------------------------------------------------------ */

.wb-stages {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
/* 六个阶段是一条流水线：用编号承担顺序感，不再堆图标 */
.stage-tab {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: 34px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-13);
  font-weight: 550;
  color: var(--text-2);
  transition:
    background var(--dur) var(--ease),
    color var(--dur) var(--ease);
}
.stage-tab:hover {
  color: var(--text-0);
  background: var(--neutral-soft);
}
.stage-no {
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-xs);
  border: 1px solid var(--line);
  background: var(--bg-1);
  font-size: var(--fs-11);
  letter-spacing: 0.02em;
  color: var(--text-3);
  flex: 0 0 auto;
  transition:
    color var(--dur) var(--ease),
    border-color var(--dur) var(--ease),
    background var(--dur) var(--ease);
}
.stage-tab:hover .stage-no {
  color: var(--text-1);
  border-color: var(--line-strong);
}
.stage-tab.active {
  color: var(--text-0);
  background: var(--accent-soft);
}
.stage-tab.active .stage-no {
  color: var(--accent-ink);
  background: var(--accent-grad);
  border-color: transparent;
}
.stage-tab.done .stage-no {
  color: var(--ok);
  border-color: rgba(111, 191, 147, 0.35);
}
.stage-tab.active.done .stage-no {
  color: var(--accent-ink);
  border-color: transparent;
}
.stage-label {
  flex: 1 1 auto;
  min-width: 0;
}
.stage-tick {
  flex: 0 0 auto;
  font-size: var(--fs-11);
  color: var(--ok);
}
.stage-gate {
  flex: 0 0 auto;
  font-size: var(--fs-11);
  color: var(--warn);
}
.stage-tab.blocked {
  color: var(--danger);
  background: var(--danger-soft);
}
.stage-tab.blocked .stage-no {
  color: var(--danger);
  border-color: rgba(224, 122, 106, 0.4);
}
.stage-tab.blocked .stage-gate {
  color: var(--danger);
}
.stage-tab.stale {
  background: var(--warn-soft);
}
.stage-tab.stale .stage-gate {
  color: var(--warn);
}

.wb-gates {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.gate-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.gate-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-xs);
  font-size: var(--fs-12);
  color: var(--text-2);
  border: 1px solid transparent;
}
.gate-item.passed {
  color: var(--text-1);
}
.gate-item.pending {
  color: var(--text-0);
  background: var(--warn-soft);
  border-color: rgba(232, 203, 109, 0.32);
}
.gate-mark {
  width: 12px;
  text-align: center;
  font-size: var(--fs-11);
  color: var(--text-3);
  flex: 0 0 auto;
}
.gate-item.passed .gate-mark {
  color: var(--ok);
}
.gate-item.pending .gate-mark {
  color: var(--warn);
}
.gate-name {
  min-width: 0;
}

/* ------------------------------------------------------------------ *
 * 右侧区
 * ------------------------------------------------------------------ */

.suggest-item,
.error-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: rgba(243, 237, 227, 0.02);
}
.error-item {
  border-color: rgba(224, 122, 106, 0.28);
  background: var(--danger-soft);
}

.log-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 320px;
  overflow: auto;
}
.log-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: rgba(243, 237, 227, 0.018);
}
.log-top {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.log-node {
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--text-1);
  max-width: 120px;
}
.log-msg {
  color: var(--text-1);
}

/* FR-004 版本记录 */
.version-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 300px;
  overflow: auto;
}
.version-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: rgba(243, 237, 227, 0.018);
}
.version-top {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.version-diff {
  border-top: 1px dashed var(--line);
  padding-top: 5px;
}
.version-diff ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.version-diff li {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  flex-wrap: wrap;
  font-size: var(--fs-12);
}
.diff-before {
  color: var(--danger);
  text-decoration: line-through;
  opacity: 0.85;
}
.diff-after {
  color: var(--ok, var(--ok));
}

/* ------------------------------------------------------------------ *
 * 底部区
 * ------------------------------------------------------------------ */

.wb-bottom {
  position: sticky;
  bottom: 0;
  z-index: 25;
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1.3fr) auto;
  gap: var(--space-5);
  align-items: center;
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--line);
  background: rgba(18, 21, 28, 0.94);
  backdrop-filter: blur(10px);
}
.wb-bottom-col {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}
.job-strip {
  display: flex;
  gap: var(--space-2);
  overflow-x: auto;
  padding-bottom: 2px;
  scrollbar-width: thin;
}
.job-chip {
  flex: 0 0 auto;
  width: 188px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
}
.job-chip-meta {
  gap: var(--space-1) 10px;
}
.job-chip-meta .ellipsis {
  max-width: 120px;
}
/* 费用说明比元信息更重要：失败时一眼能看到「没扣钱」 */
.job-cost {
  display: block;
  margin-top: 2px;
  color: var(--text-2);
}
.job-chip-stop {
  align-self: flex-start;
  padding: 1px 8px;
  font-size: var(--fs-11);
}
.wb-bottom-next {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-1);
  min-width: 0;
}
.wb-bottom-next .tiny {
  max-width: 220px;
  white-space: normal;
  text-align: right;
}

/* ------------------------------------------------------------------ *
 * 窄屏：左右侧栏改为浮层抽屉，避免把中部挤没
 * ------------------------------------------------------------------ */

@media (max-width: 1180px) {
  .wb-main,
  .workbench.side-collapsed .wb-main,
  .workbench.rail-collapsed .wb-main,
  .workbench.side-collapsed.rail-collapsed .wb-main {
    grid-template-columns: minmax(0, 1fr);
  }
  .wb-left,
  .wb-right {
    position: fixed;
    top: var(--wb-top-h, 60px);
    bottom: 0;
    z-index: 45;
    width: min(320px, 86vw);
    overflow: auto;
    background: var(--bg-1);
    box-shadow: var(--shadow-2);
  }
  .wb-left {
    left: var(--nav-w);
    border-right: 1px solid var(--line);
  }
  .wb-right {
    right: 0;
    border-left: 1px solid var(--line);
  }
  .wb-rail-inner {
    position: static;
    max-height: none;
  }
  /* 三列保持不堆叠：一列会让底栏吃掉 40% 屏高 */
  .wb-bottom {
    grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr) auto;
    align-items: end;
    gap: var(--space-3);
  }
  .wb-bottom-next {
    align-items: flex-end;
  }
}

@media (max-width: 900px) {
  .wb-top {
    padding: 8px var(--space-3) 6px;
  }
  .wb-top-row {
    flex-wrap: wrap;
    gap: var(--space-2);
  }
  .wb-top-actions {
    flex-wrap: wrap;
  }
  .wb-title-row h1 {
    max-width: 100%;
  }
  .wb-center {
    padding: var(--space-3) var(--space-3) calc(var(--wb-bottom-h, 112px) + var(--space-3));
  }
  /* 底部条只保留「任务 + 下一步」；批量操作在窄屏隐藏，避免吃掉三分之一屏 */
  .wb-bottom {
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: end;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-3);
  }
  .wb-bottom .wb-bottom-col:nth-child(2) {
    display: none;
  }
  .job-strip {
    max-height: 62px;
  }
  /* ≤900 时全局导航在顶部，抽屉要从屏幕左侧开始 */
  .wb-left {
    left: 0;
  }
  .wb-right {
    width: min(320px, 92vw);
  }
  .wb-bottom-next {
    align-items: flex-start;
  }
  .wb-bottom-next .tiny {
    text-align: left;
    max-width: none;
  }
}

@media (max-width: 760px) {
  .wb-stats {
    gap: var(--space-3);
    padding: var(--space-1) var(--space-2);
  }
  .wb-title-row h1 {
    max-width: 200px;
  }
  .wb-center {
    padding: var(--space-4);
  }
}
</style>
