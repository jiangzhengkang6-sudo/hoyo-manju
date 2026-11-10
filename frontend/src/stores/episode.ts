import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/api/errors'
import { episodesApi } from '@/api/episodes'
import { workflowApi } from '@/api/workflow'
import { fetchShots, normalizeShot } from '@/api/shots'
import { fetchJobs, isTerminalJobStatus } from '@/api/jobs'
import { onWriteActivity } from '@/api/client'
import { asList } from '@/api/projects'
import { episodeEventsUrl } from '@/api/events'
import { useSse } from '@/composables/useSse'
import { usePolling } from '@/composables/usePolling'
import { useJobWatcher } from '@/composables/useJobWatcher'
import { useJobsStore } from './jobs'
import type {
  Episode,
  EpisodeOverview,
  Job,
  ResumeInput,
  Shot,
  SseConnectionState,
  SseEvent,
  WorkflowGraphNode,
  WorkflowLogEntry,
  WorkflowRun,
} from '@/types'

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: err instanceof Error ? err.message : String(err),
    status: 0,
    retryable: true,
  })
}

/** 事件流刷新节流窗口（毫秒）：批量事件只触发一次重取。 */
const REFRESH_THROTTLE_MS = 700

/**
 * 剧集工作流状态（API.md §3 / §6 / §9）。
 * 集中持有：剧集详情、总览、工作流 run、节点图、节点日志、分镜表。
 * SSE 实时事件驱动增量刷新；断线降级为轮询。
 */
export const useEpisodeStore = defineStore('episode', () => {
  const jobsStore = useJobsStore()

  const episodeId = ref<string | null>(null)
  const episode = ref<Episode | null>(null)
  const overview = ref<EpisodeOverview | null>(null)
  const workflow = ref<WorkflowRun | null>(null)
  const graph = ref<WorkflowGraphNode[]>([])
  const logs = ref<WorkflowLogEntry[]>([])
  const shots = ref<Shot[]>([])
  /** 本剧集的任务（`GET /jobs?episode_id=`），底部「任务进度」用。 */
  const episodeJobs = ref<Job[]>([])

  const loading = ref(false)
  const refreshing = ref(false)
  const error = ref<ApiError | null>(null)
  const workflowError = ref<ApiError | null>(null)

  /* ---------------- 保存状态（顶部「保存状态」） ---------------- */

  /** 是否有写操作（保存/提交/门控决策）正在进行。 */
  const saving = ref(false)
  /** 最近一次写操作成功的时间戳（毫秒）。 */
  const lastSavedAt = ref<number | null>(null)
  /** 最近一次写操作失败的原因。 */
  const saveError = ref<string | null>(null)

  let pendingWrites = 0

  /** 订阅 api 层的写操作广播：真实反映本页的保存/提交活动，不做假状态。 */
  onWriteActivity((activity) => {
    if (activity.phase === 'start') {
      pendingWrites += 1
      saving.value = true
      saveError.value = null
      return
    }
    pendingWrites = Math.max(0, pendingWrites - 1)
    saving.value = pendingWrites > 0
    if (activity.phase === 'success') {
      lastSavedAt.value = Date.now()
      saveError.value = null
    } else {
      saveError.value = activity.message ?? '保存失败'
    }
  })

  /** 最近接收的事件（倒序，最多 40 条），用于事件流面板。 */
  const recentEvents = ref<SseEvent[]>([])
  const sseDegraded = ref(false)

  /** 本页触发的任务，SSE 断线时回退到 `GET /jobs/{id}` 轮询。 */
  const watchedJobIds = ref<string[]>([])
  // 分镜勾选状态放在 store 里：页面内的「生成分镜图」和底部批量操作栏要共用同一份，
  // 否则底部栏会按「全部未确认」生成，跟用户勾选的对不上。
  const selectedShotNos = ref<number[]>([])
  function setSelectedShots(nos: number[]): void {
    selectedShotNos.value = [...new Set(nos)].sort((a, b) => a - b)
  }

  const stage = computed(() => episode.value?.stage ?? overview.value?.stage ?? null)
  const pendingGate = computed(() => workflow.value?.pending_gate ?? overview.value?.pending_gate ?? null)
  const isBlocked = computed(() => stage.value === 'BLOCKED')
  const confirmedShotCount = computed(() => shots.value.filter((s) => Boolean(s.confirmed_media_id)).length)
  const shotCount = computed(() => shots.value.length)
  const totalDurationMs = computed(() =>
    shots.value.reduce((sum, s) => sum + (s.duration_ms ?? 0), 0),
  )

  /* ---------------- 任务观察（降级轮询） ---------------- */

  const watcher = useJobWatcher(() => watchedJobIds.value, { intervalMs: 2500 })
  const jobSnapshots = watcher.jobs

  function trackJob(job: Job | null | undefined): void {
    if (!job?.id) return
    // 先把 id 加进监听列表，再交给 watcher：否则 watcher 启动时看不到这个任务，
    // 轮询不会开始，卡片会永远停在「排队中 0%」（角色生成这类项目级任务尤其明显，
    // 它们不带 episode_id，收不到本剧集的 SSE 事件，只能靠轮询）。
    if (!watchedJobIds.value.includes(job.id)) {
      watchedJobIds.value = [...watchedJobIds.value, job.id]
    }
    watcher.track(job)
    jobsStore.upsert(job)
  }

  /* ---------------- 数据加载 ---------------- */

  let refreshTimer: number | null = null

  function scheduleRefresh(): void {
    if (refreshTimer !== null) return
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null
      void refreshLive()
    }, REFRESH_THROTTLE_MS)
  }

  async function loadEpisode(id: string, options: { silent?: boolean } = {}): Promise<void> {
    episodeId.value = id
    if (!options.silent) loading.value = true
    error.value = null
    try {
      episode.value = await episodesApi.get(id)
    } catch (err) {
      error.value = toApiError(err)
      episode.value = null
    } finally {
      if (!options.silent) loading.value = false
    }
  }

  async function loadOverview(): Promise<void> {
    if (!episodeId.value) return
    try {
      overview.value = await workflowApi.overview(episodeId.value)
    } catch {
      overview.value = null
    }
  }

  async function loadWorkflow(): Promise<void> {
    if (!episodeId.value) return
    try {
      workflow.value = await workflowApi.get(episodeId.value)
      workflowError.value = null
    } catch (err) {
      const apiErr = toApiError(err)
      // 尚未启动工作流时后端会返回 NOT_FOUND，不算错误态
      if (apiErr.status === 404 || apiErr.code === 'NOT_FOUND') {
        workflow.value = null
        workflowError.value = null
      } else {
        workflowError.value = apiErr
      }
    }
  }

  async function loadGraph(): Promise<void> {
    if (!episodeId.value) return
    try {
      const raw = await workflowApi.graph(episodeId.value)
      graph.value = Array.isArray(raw) ? raw : (raw.nodes ?? [])
    } catch {
      graph.value = []
    }
  }

  async function loadLogs(): Promise<void> {
    if (!episodeId.value) return
    try {
      logs.value = asList<WorkflowLogEntry>(await workflowApi.logs(episodeId.value))
    } catch {
      logs.value = []
    }
  }

  async function loadShots(): Promise<void> {
    if (!episodeId.value) return
    try {
      shots.value = await fetchShots(episodeId.value)
    } catch {
      shots.value = []
    }
  }

  /** 本剧集的近期任务（API.md §10 `GET /jobs?episode_id=`）。 */
  async function loadEpisodeJobs(): Promise<void> {
    if (!episodeId.value) return
    try {
      const page = await fetchJobs({ episode_id: episodeId.value, page_size: 20 })
      episodeJobs.value = page.items
    } catch {
      episodeJobs.value = []
    }
  }

  /** 首屏加载：详情 + 总览 + 工作流 + 节点图 + 分镜表 + 任务。 */
  async function loadAll(id: string): Promise<void> {
    episodeId.value = id
    loading.value = true
    error.value = null
    await loadEpisode(id)
    await Promise.all([loadOverview(), loadWorkflow(), loadGraph(), loadShots(), loadEpisodeJobs()])
    loading.value = false
  }

  /** 事件驱动的轻量刷新（不重取分镜表整表，避免抖动）。 */
  /**
   * 实时刷新：SSE 事件驱动的增量重载。
   *
   * 这里必须包含分镜表与任务列表——之前只刷 overview/workflow/episode，
   * 结果是「任务显示生成成功、候选图和画面状态却要手动刷新才出现」。
   * 分镜表是候选图、image_status、video_status 的唯一来源，漏了它等于没刷。
   */
  let refreshQueued = false

  async function refreshLive(): Promise<void> {
    if (!episodeId.value) return
    if (refreshing.value) {
      // 直接 return 会把这次事件丢掉，而调度器的定时器已经清空 → 永远不会补刷。
      // 记一个标记，等当前这轮结束后再来一次。
      refreshQueued = true
      return
    }
    refreshing.value = true
    try {
      await Promise.all([
        loadOverview(),
        loadWorkflow(),
        // silent：实时刷新不该整页闪骨架，只有首屏才需要 loading
        loadEpisode(episodeId.value, { silent: true }),
        loadShots(),
        loadEpisodeJobs(),
      ])
    } finally {
      refreshing.value = false
      if (refreshQueued) {
        refreshQueued = false
        scheduleRefresh()
      }
    }
  }

  async function refreshAll(): Promise<void> {
    await Promise.all([
      loadOverview(),
      loadWorkflow(),
      loadGraph(),
      loadLogs(),
      loadShots(),
      loadEpisodeJobs(),
    ])
  }

  /* ---------------- SSE ---------------- */

  const sse = useSse({
    url: (cursor) => (episodeId.value ? episodeEventsUrl(episodeId.value, cursor) : null),
    storageKey: 'hoyomanju:last_event_id',
    degradeAfter: 2,
    immediate: false,
    onDegraded: (value) => {
      sseDegraded.value = value
    },
    onEvent: (event) => {
      handleEvent(event)
    },
  })

  /** 连接状态与最近事件时间直接透出 useSse 的响应式引用，保持单一数据源。 */
  const sseState = computed<SseConnectionState>(() => sse.state.value)
  const lastEventAt = computed<number | null>(() => sse.lastEventAt.value)

  /**
   * 把事件里出现、但本页还没跟踪的任务纳入监听。
   *
   * 刷新页面后 `watchedJobIds` 是空的，正在跑的任务只存在于
   * `GET /jobs?episode_id=` 那份一次性的快照里——底栏读到的是它，所以
   * 进度永远停在拉取那一刻。补登记之后才会读 watcher 里的实时快照。
   */
  function adoptJob(data: Record<string, unknown>): void {
    const id = typeof data.job_id === 'string' ? data.job_id : null
    if (!id || watchedJobIds.value.includes(id)) return
    const jobEpisode = typeof data.episode_id === 'string' ? data.episode_id : null
    if (jobEpisode && episodeId.value && jobEpisode !== episodeId.value) return
    watchedJobIds.value = [...watchedJobIds.value, id]
    const snapshot = jobSnapshots.value[id]
    if (snapshot && !isTerminalJobStatus(snapshot.status)) watcher.start()
  }

  /** SSE 事件分发（API.md §10.1 事件类型）。 */
  function handleEvent(event: SseEvent): void {
    recentEvents.value = [event, ...recentEvents.value].slice(0, 40)

    switch (event.type) {
      case 'job.created':
      case 'job.status_changed':
      case 'job.progress':
      case 'job.succeeded':
      case 'job.failed':
      case 'job.cancelled':
        watcher.applyEvent(event.data)
        jobsStore.applyEvent(event.data)
        adoptJob(event.data)
        if (event.type !== 'job.progress') scheduleRefresh()
        break
      case 'workflow.gate_waiting':
      case 'workflow.resumed':
      case 'workflow.node_changed':
      case 'episode.stage_changed':
        scheduleRefresh()
        break
      case 'shot.confirmed':
        applyShotConfirmedEvent(event.data)
        break
      case 'media.created':
        // 候选图落库就该立刻出现。以前加了 `shotCount > 0` 的前置判断，
        // 分镜表还没加载时（首屏/刚切集）这次事件就被吞掉了。
        scheduleRefresh()
        break
      case 'render.progress':
        scheduleRefresh()
        break
      default:
        break
    }
  }

  function applyShotConfirmedEvent(data: Record<string, unknown>): void {
    const shotNo = typeof data.shot_no === 'number' ? data.shot_no : null
    const mediaId = typeof data.media_id === 'string' ? data.media_id : null
    if (shotNo === null) return
    shots.value = shots.value.map((s) =>
      s.shot_no === shotNo ? { ...s, confirmed_media_id: mediaId ?? s.confirmed_media_id } : s,
    )
  }

  function connectEvents(): void {
    if (!episodeId.value) return
    sseDegraded.value = false
    sse.start()
  }

  function disconnectEvents(): void {
    sse.stop()
  }

  /* ---------------- 降级轮询 ---------------- */

  const poller = usePolling(
    async () => {
      await refreshLive()
      // 未结束任务并行补一次状态，避免 SSE 断开时进度条卡住
      await watcher.refresh()
    },
    {
      intervalMs: 4000,
      immediate: false,
      shouldRun: () => sseDegraded.value,
    },
  )

  /** 由布局层在挂载/卸载时调用。 */
  function startLive(): void {
    connectEvents()
    poller.start()
  }

  function stopLive(): void {
    poller.stop()
    watcher.stop()
    disconnectEvents()
  }

  /* ---------------- 门控 / 工作流操作 ---------------- */

  async function startWorkflow(): Promise<WorkflowRun | null> {
    if (!episodeId.value) return null
    const run = await workflowApi.start(episodeId.value)
    workflow.value = run
    await refreshLive()
    return run
  }

  /** 门控决策；版本不一致会返回 CHECKPOINT_STALE，由调用方展示重试。 */
  async function resumeGate(
    decision: ResumeInput['decision'],
    payload?: Record<string, unknown>,
    targetVersion?: number,
  ): Promise<WorkflowRun | null> {
    if (!episodeId.value) return null
    if (!workflow.value?.id || !workflow.value.checkpoint_id || !workflow.value.pending_gate) {
      throw new ApiError({
        code: 'STATE_CONFLICT',
        message: '当前没有待确认的门控，请刷新工作流状态后重试',
        status: 409,
        retryable: true,
      })
    }
    const body: ResumeInput = {
      workflow_run_id: workflow.value.id,
      checkpoint_id: workflow.value.checkpoint_id,
      gate: workflow.value.pending_gate,
      decision,
      payload,
      target_version: targetVersion,
    }
    const run = await workflowApi.resume(episodeId.value, body)
    workflow.value = run
    await refreshLive()
    return run
  }

  async function retryNode(node: string): Promise<WorkflowRun | null> {
    if (!episodeId.value) return null
    const run = await workflowApi.retry(episodeId.value, node)
    workflow.value = run
    await refreshLive()
    return run
  }

  async function rollbackTo(toNode: string): Promise<WorkflowRun | null> {
    if (!episodeId.value) return null
    const run = await workflowApi.rollback(episodeId.value, toNode)
    workflow.value = run
    await refreshLive()
    return run
  }

  /** 切换剧集时清理上一次的状态，避免串数据。 */
  function reset(): void {
    stopLive()
    episodeId.value = null
    episode.value = null
    overview.value = null
    workflow.value = null
    graph.value = []
    logs.value = []
    shots.value = []
    episodeJobs.value = []
    recentEvents.value = []
    watchedJobIds.value = []
    error.value = null
    workflowError.value = null
    saveError.value = null
    sseDegraded.value = false
  }

  /** 本地更新单个分镜（接口返回后同步到列表）。 */
  function patchLocalShot(updated: Shot): void {
    const idx = shots.value.findIndex((s) => s.id === updated.id)
    if (idx < 0) return
    // 接口返回的是「行 + shot_json」，store 里存的是扁平对象，合并前先摊平，
    // 否则保存后切镜头/重载就看不到刚改的内容。
    const flat = normalizeShot(updated)
    const next = [...shots.value]
    next[idx] = { ...shots.value[idx], ...flat }
    shots.value = next
  }

  return {
    episodeId,
    episode,
    overview,
    workflow,
    graph,
    logs,
    shots,
    episodeJobs,
    loading,
    refreshing,
    error,
    workflowError,
    saving,
    lastSavedAt,
    saveError,
    recentEvents,
    sseState,
    sseDegraded,
    lastEventAt,
    lastEventId: sse.lastEventId,
    eventCount: sse.eventCount,
    watchedJobIds,
    selectedShotNos,
    setSelectedShots,
    jobSnapshots,
    stage,
    pendingGate,
    isBlocked,
    shotCount,
    confirmedShotCount,
    totalDurationMs,
    trackJob,
    loadEpisode,
    loadOverview,
    loadWorkflow,
    loadGraph,
    loadLogs,
    loadShots,
    loadEpisodeJobs,
    loadAll,
    refreshLive,
    refreshAll,
    startLive,
    stopLive,
    startWorkflow,
    resumeGate,
    retryNode,
    rollbackTo,
    reset,
    patchLocalShot,
  }
})
