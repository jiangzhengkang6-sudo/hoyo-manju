/**
 * 分镜「行 + shot_json」两层结构的摊平回归。
 *
 * 线上现象：保存了镜头，切到别的镜头再切回来（或刷新），编辑区又变空。
 * 数据其实存住了，是读取时没把 shot_json 摊到顶层，`shot.action` 恒为 undefined。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  del: vi.fn(),
}))

vi.mock('./client', () => ({ http: mocks }))

import { fetchCandidates, fetchShots, normalizeCandidate, normalizeShot } from './shots'

beforeEach(() => {
  mocks.get.mockReset()
  mocks.post.mockReset()
  mocks.patch.mockReset()
})

describe('normalizeShot', () => {
  it('把 shot_json 里的字段摊到顶层', () => {
    const shot = normalizeShot({
      id: 'sht_1',
      shot_no: 1,
      duration_ms: 4000,
      shot_json: { action: '推门而入', image_prompt: '雨夜街道' },
    } as never)
    expect(shot.action).toBe('推门而入')
    expect(shot.image_prompt).toBe('雨夜街道')
  })

  it('行上的字段优先（duration_ms 以行为准）', () => {
    const shot = normalizeShot({
      id: 'sht_1',
      shot_no: 1,
      duration_ms: 6000,
      shot_json: { duration_ms: 4000 },
    } as never)
    expect(shot.duration_ms).toBe(6000)
  })

  it('缺少 shot_json 时不炸', () => {
    const shot = normalizeShot({ id: 'sht_1', shot_no: 1, duration_ms: 4000 } as never)
    expect(shot.action).toBeUndefined()
    expect(shot.id).toBe('sht_1')
  })

  it('候选图逐项补齐 media_id（否则「生成成功但显示暂无素材」）', () => {
    // 后端只给 id；卡片按 media_id 找图。第 1 镜之所以正常，是因为它已确认、
    // 走 confirmed_media_id 那条路，掩盖了候选数组没归一的问题。
    const shot = normalizeShot({
      id: 'sht_2',
      shot_no: 2,
      duration_ms: 4000,
      image_status: 'CANDIDATES_READY',
      confirmed_media_id: null,
      candidates: [
        { id: 'med_1', safety_status: 'PASSED' },
        { id: 'med_2', safety_status: 'PASSED' },
      ],
    } as never)
    expect(shot.candidates?.map((c) => c.media_id)).toEqual(['med_1', 'med_2'])
    expect(shot.candidates?.[0].safety).toBe('PASSED')
  })
})

describe('fetchShots', () => {
  it('列表返回的对象已经摊平（切镜头/刷新后仍能看到内容）', async () => {
    mocks.get.mockResolvedValueOnce([
      { id: 'sht_2', shot_no: 2, duration_ms: 4000, shot_json: { action: '第二个镜头' } },
      { id: 'sht_1', shot_no: 1, duration_ms: 4000, shot_json: { action: '第一个镜头' } },
    ])
    const shots = await fetchShots('epi_1')
    expect(shots.map((s) => s.shot_no)).toEqual([1, 2])
    expect(shots[0].action).toBe('第一个镜头')
  })
})

describe('normalizeCandidate', () => {
  it('把 safety 对象拍平成状态字符串（否则界面直接渲染一坨 JSON）', () => {
    const cand = normalizeCandidate({
      media_id: 'med_1',
      safety: { status: 'PASSED', detail: { checked: true } },
    } as never)
    expect(cand.safety).toBe('PASSED')
    expect(cand.safety_detail).toEqual({ checked: true })
    expect(cand.broken).toBe(false)
  })

  it('兼容旧的字符串 safety 与扁平 safety_status', () => {
    expect(normalizeCandidate({ media_id: 'm', safety: 'BLOCKED' } as never).safety).toBe('BLOCKED')
    expect(
      normalizeCandidate({ media_id: 'm', safety_status: 'REVIEW' } as never).safety,
    ).toBe('REVIEW')
  })

  it('带 stale_reason 的素材标记为 broken（不可选、不可恢复）', () => {
    const cand = normalizeCandidate({
      media_id: 'med_2',
      stale_reason: '体积不符：库里 10B，实际 20B',
    } as never)
    expect(cand.broken).toBe(true)
  })

  it('后端主键 id 补成 media_id（不然选/驳回都提交 undefined）', () => {
    const cand = normalizeCandidate({ id: 'med_9', safety_status: 'PASSED' } as never)
    expect(cand.media_id).toBe('med_9')
    expect(cand.safety).toBe('PASSED')
  })
})

describe('fetchCandidates', () => {
  it('接口返回的数据统一过归一化', async () => {
    mocks.get.mockResolvedValueOnce([
      { media_id: 'med_1', safety: { status: 'PASSED' }, safety_status: 'PASSED' },
    ])
    const items = await fetchCandidates('sht_1')
    expect(items[0].safety).toBe('PASSED')
  })
})
