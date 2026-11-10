/**
 * 角色接口路径回归。
 *
 * 「剧本同步角色」是后端新加的剧集级接口（`POST /episodes/{id}/characters/sync`），
 * 路径写错时前端不会报类型错，只会在页面上变成 404 —— 这类错最容易漏。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  get: vi.fn(async (path: string) => ({ path })),
  post: vi.fn(async (path: string) => ({ path })),
  patch: vi.fn(async (path: string) => ({ path })),
  del: vi.fn(async (path: string) => ({ path })),
}))

vi.mock('./client', () => ({ http: mocks }))

import { charactersApi } from './characters'

beforeEach(() => {
  mocks.post.mockClear()
})

describe('charactersApi.syncFromEpisode', () => {
  it('打到剧集级同步接口（不是项目级）', async () => {
    await charactersApi.syncFromEpisode('epi_abc')
    expect(mocks.post).toHaveBeenCalledWith('/episodes/epi_abc/characters/sync')
  })

  it('force 时带上覆盖参数', async () => {
    await charactersApi.syncFromEpisode('epi_abc', true)
    expect(mocks.post).toHaveBeenCalledWith('/episodes/epi_abc/characters/sync?force=true')
  })
})

describe('fetchCharacterAssets', () => {
  it('把后端素材行的 id 归一成 media_id（否则「设为主图」会提交空 body → 422）', async () => {
    const { fetchCharacterAssets } = await import('./characters')
    mocks.get.mockResolvedValueOnce([{ id: 'med_1', version: 1, asset_kind: 'front' }] as never)
    const [asset] = await fetchCharacterAssets('chr_1')
    expect(asset.media_id).toBe('med_1')
    expect(asset.id).toBe('med_1')
  })

  it('后端已经给了 media_id 时保持不变', async () => {
    const { fetchCharacterAssets } = await import('./characters')
    mocks.get.mockResolvedValueOnce([{ id: 'med_2', media_id: 'med_2', version: 1 }] as never)
    const [asset] = await fetchCharacterAssets('chr_1')
    expect(asset.media_id).toBe('med_2')
  })
})
