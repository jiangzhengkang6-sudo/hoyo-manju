/**
 * 任务观察器回归：track() 之后必须真的开始轮询。
 *
 * 线上现象：角色参考图生成成功了（图都落盘了），但任务卡一直显示「排队中 0%」、
 * 角色卡也不刷新 —— 因为 `start()` 在「此刻还没有 id」时提前 return，
 * 而调用方是「先 track(job) 再登记 id」，于是轮询永远没启动。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const jobsGet = vi.hoisted(() => vi.fn())

vi.mock('@/api/jobs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/jobs')>()
  return {
    ...actual,
    jobsApi: { ...actual.jobsApi, get: jobsGet },
  }
})

import { useJobWatcher } from './useJobWatcher'
import type { Job } from '@/types'

function job(id: string, status: string): Job {
  return { id, status, type: 'CHARACTER_ASSET_GENERATION', progress: 0 } as Job
}

beforeEach(() => {
  jobsGet.mockReset()
})

describe('useJobWatcher', () => {
  it('track 一个未结束任务后会轮询到终态', async () => {
    vi.useFakeTimers()
    const ids: string[] = []
    const watcher = useJobWatcher(() => ids)

    jobsGet.mockResolvedValue(job('job_1', 'SUCCEEDED'))
    watcher.track(job('job_1', 'QUEUED'))
    ids.push('job_1') // 模拟 store「先 track 后登记 id」

    await vi.advanceTimersByTimeAsync(2600)
    expect(jobsGet).toHaveBeenCalledWith('job_1')
    expect(watcher.jobs.value['job_1'].status).toBe('SUCCEEDED')
    vi.useRealTimers()
  })

  it('任务已是终态时不再轮询', async () => {
    vi.useFakeTimers()
    const watcher = useJobWatcher(() => ['job_2'])
    watcher.track(job('job_2', 'SUCCEEDED'))
    await vi.advanceTimersByTimeAsync(6000)
    expect(jobsGet).not.toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('没有任务时启动一次也不会发请求', async () => {
    vi.useFakeTimers()
    const watcher = useJobWatcher(() => [])
    watcher.start()
    await vi.advanceTimersByTimeAsync(6000)
    expect(jobsGet).not.toHaveBeenCalled()
    expect(watcher.active.value).toBe(false)
    vi.useRealTimers()
  })
})
