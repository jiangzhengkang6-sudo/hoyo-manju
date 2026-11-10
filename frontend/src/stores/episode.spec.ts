/**
 * 剧集 store 的实时事件应用与派生状态。
 *
 * 这里盯的是「SSE 事件到达后本地状态有没有被正确改动」——
 * 这类代码一旦回退，页面会静默显示过期内容（比报错更难发现）。
 *
 * 所有会发网络请求的 composable / api 都被替身化，只保留被测的状态逻辑。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

import type { Job, Shot, SseEvent } from '@/types'

/**
 * `handleEvent` 没有对外暴露（SSE 回调内部调用），
 * 所以这里把传给 `useSse` 的 `onEvent` 捕获下来，
 * 通过它驱动事件分发 —— 与真实运行路径一致。
 */
const hoisted = vi.hoisted(() => ({
  onEvent: { current: null as null | ((event: unknown) => void) },
  onDegraded: { current: null as null | ((value: boolean) => void) },
  track: vi.fn(),
  applyEvent: vi.fn(),
  start: vi.fn(),
  stop: vi.fn(),
}))

vi.mock('@/composables/useSse', () => ({
  useSse: (opts: { onEvent?: (e: unknown) => void; onDegraded?: (v: boolean) => void }) => {
    hoisted.onEvent.current = opts.onEvent ?? null
    hoisted.onDegraded.current = opts.onDegraded ?? null
    return {
      state: ref('idle'),
      lastEventAt: ref<number | null>(null),
      lastEventId: ref<string | null>(null),
      eventCount: ref(0),
      start: hoisted.start,
      stop: hoisted.stop,
    }
  },
}))

vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({
    active: ref(false),
    running: ref(false),
    lastError: ref(null),
    ticks: ref(0),
    start: vi.fn(),
    stop: vi.fn(),
    refresh: vi.fn(),
  }),
}))

vi.mock('@/composables/useJobWatcher', () => ({
  useJobWatcher: () => ({
    jobs: ref({}),
    active: ref(false),
    track: hoisted.track,
    applyEvent: hoisted.applyEvent,
    refresh: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
  }),
}))

vi.mock('@/api/client', () => ({
  onWriteActivity: vi.fn(),
  reportError: vi.fn(),
}))

vi.mock('@/api/episodes', () => ({ episodesApi: { get: vi.fn(), overview: vi.fn() } }))
vi.mock('@/api/workflow', () => ({ workflowApi: {} }))
vi.mock('@/api/shots', () => ({ fetchShots: vi.fn(async () => []) }))
vi.mock('@/api/jobs', () => ({
  fetchJobs: vi.fn(async () => []),
  isTerminalJobStatus: (status?: string | null) =>
    status === 'SUCCEEDED' || status === 'FAILED' || status === 'CANCELLED' || status === 'EXPIRED',
}))
vi.mock('@/api/projects', () => ({ asList: (v: unknown) => (Array.isArray(v) ? v : []) }))
vi.mock('@/api/events', () => ({ episodeEventsUrl: () => '/api/v1/events' }))
vi.mock('@/stores/jobs', () => ({
  useJobsStore: () => ({ upsert: vi.fn(), applyEvent: vi.fn() }),
}))

import { useEpisodeStore } from '@/stores/episode'

/** 走真实路径：把事件喂给 store 注册的 SSE 回调。 */
function emit(type: string, data: Record<string, unknown> = {}): void {
  const handler = hoisted.onEvent.current
  if (!handler) throw new Error('store 未注册 onEvent')
  handler({ id: '1', type, data, receivedAt: Date.now() } as SseEvent)
}

function shot(shotNo: number, extra: Partial<Shot> = {}): Shot {
  return {
    id: `sht_${shotNo}`,
    episode_id: 'epi_1',
    shot_no: shotNo,
    duration_ms: 4000,
    ...extra,
  } as Shot
}

describe('episode store 派生状态', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    hoisted.track.mockClear()
    hoisted.applyEvent.mockClear()
  })

  it('总时长按分镜时长求和，缺字段按 0 计', () => {
    const store = useEpisodeStore()
    store.shots = [shot(1), shot(2, { duration_ms: 3500 }), shot(3, { duration_ms: undefined as never })]
    expect(store.totalDurationMs).toBe(7500)
  })

  it('已确认分镜数只统计有 confirmed_media_id 的', () => {
    const store = useEpisodeStore()
    store.shots = [
      shot(1, { confirmed_media_id: 'med_1' }),
      shot(2, { confirmed_media_id: null }),
      shot(3, { confirmed_media_id: 'med_3' }),
    ]
    expect(store.shotCount).toBe(3)
    expect(store.confirmedShotCount).toBe(2)
  })

  it('stage / pendingGate 优先取 episode，缺失时回落到 overview', () => {
    const store = useEpisodeStore()
    store.overview = { stage: 'STORY', pending_gate: 'gate_script_confirmation' } as never
    expect(store.stage).toBe('STORY')
    expect(store.pendingGate).toBe('gate_script_confirmation')

    store.episode = { stage: 'RENDER' } as never
    expect(store.stage).toBe('RENDER')
  })

  it('isBlocked 只在 BLOCKED 阶段为真', () => {
    const store = useEpisodeStore()
    store.overview = { stage: 'BLOCKED' } as never
    expect(store.isBlocked).toBe(true)
    store.overview = { stage: 'STORY' } as never
    expect(store.isBlocked).toBe(false)
  })
})

describe('shot.confirmed 事件应用', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('按镜号把 confirmed_media_id 落到对应分镜，其它分镜不动', () => {
    const store = useEpisodeStore()
    store.shots = [shot(1), shot(2), shot(3)]

    emit('shot.confirmed', { shot_no: 2, media_id: 'med_new' })

    expect(store.shots[0].confirmed_media_id).toBeUndefined()
    expect(store.shots[1].confirmed_media_id).toBe('med_new')
    expect(store.shots[2].confirmed_media_id).toBeUndefined()
  })

  it('事件缺 shot_no 时整个忽略，不误改任何分镜', () => {
    const store = useEpisodeStore()
    store.shots = [shot(1, { confirmed_media_id: 'med_old' })]

    emit('shot.confirmed', { media_id: 'med_new' })

    expect(store.shots[0].confirmed_media_id).toBe('med_old')
  })

  it('事件缺 media_id 时保留原值，不把已确认的图清掉', () => {
    const store = useEpisodeStore()
    store.shots = [shot(1, { confirmed_media_id: 'med_old' })]

    emit('shot.confirmed', { shot_no: 1 })

    expect(store.shots[0].confirmed_media_id).toBe('med_old')
  })

  it('镜号不存在时不新增分镜（只做就地更新）', () => {
    const store = useEpisodeStore()
    store.shots = [shot(1)]

    emit('shot.confirmed', { shot_no: 99, media_id: 'med_x' })

    expect(store.shots).toHaveLength(1)
  })
})

describe('事件流面板与任务事件分发', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    hoisted.applyEvent.mockClear()
  })

  it('最近事件倒序保存，最多 40 条', () => {
    const store = useEpisodeStore()
    for (let i = 1; i <= 45; i += 1) {
      emit('workflow.node_changed', { n: i })
    }
    expect(store.recentEvents).toHaveLength(40)
    // 最新一条在最前
    expect((store.recentEvents[0].data as { n: number }).n).toBe(45)
  })

  it('任务类事件转交给 job watcher', () => {
    useEpisodeStore()
    emit('job.succeeded', { id: 'job_1', status: 'SUCCEEDED' })
    expect(hoisted.applyEvent).toHaveBeenCalledTimes(1)
    expect(hoisted.applyEvent.mock.calls[0][0]).toMatchObject({ id: 'job_1' })
  })

  it('未知事件类型不抛异常', () => {
    useEpisodeStore()
    expect(() => emit('some.unknown.event', {})).not.toThrow()
  })
})

describe('trackJob', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    hoisted.track.mockClear()
  })

  it('记录任务并去重', () => {
    const store = useEpisodeStore()
    const job = { id: 'job_1', status: 'RUNNING' } as Job

    store.trackJob(job)
    store.trackJob(job)

    expect(hoisted.track).toHaveBeenCalledTimes(2)
    expect(store.watchedJobIds).toEqual(['job_1'])
  })

  it('空任务被忽略', () => {
    const store = useEpisodeStore()
    store.trackJob(null)
    store.trackJob(undefined)
    expect(store.watchedJobIds).toEqual([])
  })
})

describe('selectedShotNos（分镜勾选共享状态）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('去重并升序，保证底部批量操作与页面勾选一致', () => {
    const store = useEpisodeStore()
    store.setSelectedShots([3, 1, 3, 2])
    expect(store.selectedShotNos).toEqual([1, 2, 3])
  })

  it('可以清空（未勾选时生成按钮应提示而不是默认全选）', () => {
    const store = useEpisodeStore()
    store.setSelectedShots([1, 2])
    store.setSelectedShots([])
    expect(store.selectedShotNos).toEqual([])
  })
})

/**
 * 实时刷新范围：SSE 事件到了，页面上的东西必须自己更新。
 *
 * 历史 bug：`refreshLive()` 只刷 overview/workflow/episode，不含分镜表，
 * 于是「任务显示生成成功、候选图仍是暂无素材」，用户必须手动刷新。
 */
describe('SSE 事件触发的刷新范围', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  async function wait(ms: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms))
  }

  it('media.created 会重载分镜表（新图不再需要手动刷新）', async () => {
    const shotsApi = await import('@/api/shots')
    const fetchShots = vi.mocked(shotsApi.fetchShots)
    fetchShots.mockClear()

    const store = useEpisodeStore()
    store.episodeId = 'epi_1'
    emit('media.created', { media_id: 'med_1', kind: 'image', role: 'shot_image' })

    // 刷新有 700ms 节流，等它落地
    await wait(900)
    expect(fetchShots).toHaveBeenCalledWith('epi_1')
  })

  it('分镜表还没加载（shotCount=0）时也刷新，不能因为空就吞掉事件', async () => {
    const shotsApi = await import('@/api/shots')
    const fetchShots = vi.mocked(shotsApi.fetchShots)
    fetchShots.mockClear()

    const store = useEpisodeStore()
    store.episodeId = 'epi_1'
    store.shots = []
    emit('media.created', { media_id: 'med_2', kind: 'image' })

    await wait(900)
    expect(fetchShots).toHaveBeenCalled()
  })

  it('刷新过程中收到的事件不会被丢掉（合并成末尾再刷一次）', async () => {
    const shotsApi = await import('@/api/shots')
    const fetchShots = vi.mocked(shotsApi.fetchShots)
    // 让第一次刷新慢一点，制造「刷新中又来了事件」的窗口
    const pending: { resolve: (() => void) | null } = { resolve: null }
    fetchShots.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          pending.resolve = () => resolve([])
        }) as never,
    )
    fetchShots.mockClear()

    const store = useEpisodeStore()
    store.episodeId = 'epi_1'
    emit('job.succeeded', { job_id: 'job_a', status: 'SUCCEEDED' })
    await wait(800) // 第一次刷新已开始，卡在 fetchShots 上
    emit('job.succeeded', { job_id: 'job_b', status: 'SUCCEEDED' })

    pending.resolve?.()
    await wait(1200)
    // 第二次事件应该排队补刷，而不是被 `if (refreshing) return` 吞掉
    expect(fetchShots.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('页面刷新后进来的任务会被纳入监听（否则进度停在初始快照）', () => {
    const store = useEpisodeStore()
    store.episodeId = 'epi_1'
    expect(store.watchedJobIds).not.toContain('job_late')

    emit('job.progress', { job_id: 'job_late', status: 'RUNNING', progress: 40, episode_id: 'epi_1' })

    expect(store.watchedJobIds).toContain('job_late')
  })

  it('其它剧集的任务不会被误纳入', () => {
    const store = useEpisodeStore()
    store.episodeId = 'epi_1'
    emit('job.progress', { job_id: 'job_other', status: 'RUNNING', episode_id: 'epi_2' })
    expect(store.watchedJobIds).not.toContain('job_other')
  })
})
