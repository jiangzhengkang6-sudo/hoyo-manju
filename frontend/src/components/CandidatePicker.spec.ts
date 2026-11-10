/**
 * 候选图选择区：把「技术上正确但没人看得懂」的东西挡在界面之外。
 *
 * 线上现象：每张候选下面写着 `v3 · {"status":"PASSED","checks":…}`，
 * 以及「素材文件异常：体积不符：库里 785552B，实际 770800B」——
 * 前者是 safety 对象被当成字符串渲染，后者是文件被覆盖后的技术细节。
 * 这里钉住：状态必须是中文标签、失效候选必须收起且不可选。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/client', () => ({ downloadFile: vi.fn(), reportError: vi.fn() }))

import CandidatePicker from '@/components/CandidatePicker.vue'
import type { Shot, ShotCandidate } from '@/types'

const shot = {
  id: 'sht_1',
  shot_no: 1,
  duration_ms: 4000,
  selected_media_id: null,
  confirmed_media_id: null,
} as unknown as Shot

const healthy: ShotCandidate = {
  media_id: 'med_ok',
  version: 3,
  status: 'CANDIDATE',
  safety: 'PASSED',
  usable: true,
  url: '/x.png',
}

const broken: ShotCandidate = {
  media_id: 'med_bad',
  version: 3,
  status: 'REJECTED',
  safety: 'PASSED',
  usable: false,
  broken: true,
  stale_reason: '体积不符：库里 785552B，实际 770800B',
  url: '/y.png',
}

function mountPicker(candidates: ShotCandidate[]) {
  return mount(CandidatePicker, {
    props: { shot, candidates, busy: false },
  })
}

describe('CandidatePicker', () => {
  it('安全检查显示中文标签，不把对象/英文枚举直接倒出来', () => {
    const wrapper = mountPicker([healthy])
    expect(wrapper.text()).toContain('安全通过')
    expect(wrapper.text()).not.toContain('{')
    expect(wrapper.text()).not.toContain('PASSED')
  })

  it('失效候选不进入可选网格，而是收在折叠区里', () => {
    const wrapper = mountPicker([healthy, broken])
    expect(wrapper.findAll('.cp-item')).toHaveLength(1)
    expect(wrapper.text()).toContain('查看 1 张已失效候选')
    expect(wrapper.text()).not.toContain('体积不符')      // 技术细节默认不展示
    expect(wrapper.text()).not.toContain('785552B')
  })

  it('展开失效候选时用人话解释原因', async () => {
    const wrapper = mountPicker([healthy, broken])
    await wrapper.find('.cp-broken button').trigger('click')
    expect(wrapper.text()).toContain('与记录不一致')
    expect(wrapper.text()).not.toContain('785552B')
  })

  it('已驳回的候选点了也不会被选中', async () => {
    const rejected: ShotCandidate = { ...healthy, media_id: 'med_rej', status: 'REJECTED', usable: false }
    const wrapper = mountPicker([rejected])
    await wrapper.find('.cp-item').trigger('click')
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('正常候选点击会触发选中', async () => {
    const wrapper = mountPicker([healthy])
    await wrapper.find('.cp-item').trigger('click')
    expect(wrapper.emitted('select')?.[0]).toEqual(['med_ok'])
  })

  it('驳回当前选中的图后，本地选中状态一并清掉', async () => {
    const wrapper = mountPicker([healthy])
    await wrapper.find('.cp-item').trigger('click')
    await wrapper.find('.cp-actions button').trigger('click')
    await wrapper.find('.cp-reject textarea').setValue('人物脸崩')
    await wrapper.find('.cp-reject .btn-danger').trigger('click')
    expect(wrapper.emitted('reject')?.[0]).toEqual(['med_ok', '人物脸崩'])
    expect(wrapper.text()).not.toContain('已选')
  })
})
