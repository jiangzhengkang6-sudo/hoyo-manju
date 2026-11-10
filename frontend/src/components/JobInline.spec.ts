/**
 * 行内任务卡的「停止生成」：
 * - 运行中可点，发出 cancel；
 * - 已请求停止（CANCELLING）时按钮禁用并显示「停止中…」，避免连点；
 * - 终态不再显示按钮。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({ downloadFile: vi.fn(), reportError: vi.fn() }))

import JobInline from '@/components/JobInline.vue'
import type { Job } from '@/types'

function makeJob(status: string): Job {
  return {
    id: 'job_1',
    type: 'IMAGE_GENERATION',
    status,
    progress: 40,
    attempt: 1,
    max_attempts: 2,
  } as unknown as Job
}

describe('JobInline 停止按钮', () => {
  it('运行中显示「停止生成」并派发 cancel', async () => {
    const wrapper = mount(JobInline, { props: { job: makeJob('RUNNING') } })
    const button = wrapper.findAll('button').find((b) => b.text().includes('停止生成'))
    expect(button).toBeTruthy()
    await button!.trigger('click')
    expect(wrapper.emitted('cancel')?.[0]).toEqual(['job_1'])
  })

  it('取消中按钮禁用，文案变「停止中…」', async () => {
    const wrapper = mount(JobInline, { props: { job: makeJob('CANCELLING') } })
    const button = wrapper.findAll('button').find((b) => b.text().includes('停止中'))
    expect(button).toBeTruthy()
    expect(button!.attributes('disabled')).toBeDefined()
    await button!.trigger('click')
    expect(wrapper.emitted('cancel')).toBeUndefined()
  })

  it('已完成的任务不显示停止按钮', () => {
    const wrapper = mount(JobInline, { props: { job: makeJob('SUCCEEDED') } })
    expect(wrapper.text()).not.toContain('停止')
  })
})
