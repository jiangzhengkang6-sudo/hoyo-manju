import { http } from './client'
import { asList } from './projects'
import type {
  Character,
  CharacterAsset,
  CharacterGenerateInput,
  CharacterInput,
  CharacterSyncResult,
  Job,
  ReplacePrimaryResult,
  StyleApplyResult,
  StyleAsset,
} from '@/types'

/** 角色与画风（API.md §5）。 */
export const charactersApi = {
  list: (projectId: string, query = '') =>
    http.get<Character[] | { items: Character[]; hidden?: number }>(
      `/projects/${projectId}/characters${query ? `?${query}` : ''}`,
    ),

  create: (projectId: string, body: CharacterInput) =>
    http.post<Character>(`/projects/${projectId}/characters`, body),

  get: (characterId: string) => http.get<Character>(`/characters/${characterId}`),

  patch: (characterId: string, body: Partial<CharacterInput>) =>
    http.patch<Character>(`/characters/${characterId}`, body),

  /**
   * 从剧集最新剧本抓取角色（幂等）。
   * 默认不覆盖用户手改过的字段，`force=true` 才用人物表整体覆盖。
   */
  syncFromEpisode: (episodeId: string, force = false) =>
    http.post<CharacterSyncResult>(
      `/episodes/${episodeId}/characters/sync${force ? '?force=true' : ''}`,
    ),

  /** 归档角色。 */
  archive: (characterId: string) => http.del<Character>(`/characters/${characterId}`),

  /** 生成角色参考图（异步 job）。 */
  generate: (characterId: string, body: CharacterGenerateInput) =>
    http.post<Job>(`/characters/${characterId}/generate`, body),

  /** 确认主参考图。 */
  confirm: (characterId: string, mediaId: string) =>
    http.post<Character>(`/characters/${characterId}/confirm`, { media_id: mediaId }),

  /** 替换主参考图，返回受影响分镜列表。 */
  replacePrimary: (characterId: string, mediaId: string, impact: 'future_only' | 'mark_stale') =>
    http.post<ReplacePrimaryResult>(`/characters/${characterId}/replace-primary`, {
      media_id: mediaId,
      impact,
    }),

  assets: (characterId: string) =>
    http.get<CharacterAsset[] | { items: CharacterAsset[] }>(`/characters/${characterId}/assets`),

  /**
   * 绑定音色（API.md §8 / FR-050）。
   * `speed`（FR-051，0.5~2.0）给了就一并更新；换音色或改语速都会让已有配音过期。
   */
  bindVoice: (characterId: string, voiceId: string, speed?: number) =>
    http.post<Character>(`/characters/${characterId}/voice`, {
      voice_id: voiceId,
      ...(speed === undefined ? {} : { speed }),
    }),
}

export const stylesApi = {
  list: (projectId: string) =>
    http.get<StyleAsset[] | { items: StyleAsset[] }>(`/projects/${projectId}/styles`),

  create: (projectId: string, body: Partial<StyleAsset>) =>
    http.post<StyleAsset>(`/projects/${projectId}/styles`, body),

  patch: (styleId: string, body: Partial<StyleAsset>) =>
    http.patch<StyleAsset>(`/styles/${styleId}`, body),

  /**
   * 应用到项目（设为 `default_style_id`）。
   * 后端返回的是 `{project_id, style_id, affected_shots}`（不是 StyleAsset）——
   * `affected_shots` 是「切换画风会把多少分镜标记过期」，界面必须能告知用户。
   */
  apply: (styleId: string) => http.post<StyleApplyResult>(`/styles/${styleId}/apply`),
}

export interface CharacterListOptions {
  /** 只看这一集剧本里出现的角色（项目里可能有多集互不相干的故事）。 */
  episodeId?: string
  /** 连其他剧集的角色一起看。 */
  includeAll?: boolean
}

export interface CharacterListPage {
  items: Character[]
  /** 被「按剧集过滤」挡掉的其他剧集角色数。 */
  hidden: number
}

export async function fetchCharacterList(
  projectId: string,
  { episodeId, includeAll }: CharacterListOptions = {},
): Promise<CharacterListPage> {
  const params = new URLSearchParams()
  if (episodeId) params.set('episode_id', episodeId)
  if (includeAll) params.set('include_all', 'true')
  const data = await charactersApi.list(projectId, params.toString())
  return {
    items: asList<Character>(data),
    // 老后端只返回数组时按「没有隐藏项」处理
    hidden: Array.isArray(data) ? 0 : Number(data.hidden ?? 0),
  }
}

export async function fetchCharacters(
  projectId: string,
  options: CharacterListOptions = {},
): Promise<Character[]> {
  return (await fetchCharacterList(projectId, options)).items
}

export async function fetchCharacterAssets(characterId: string): Promise<CharacterAsset[]> {
  const raw = asList<CharacterAsset & { id?: string }>(await charactersApi.assets(characterId))
  // 后端媒体行主键叫 `id`，前端契约叫 `media_id`。老版本后端不返回别名时，
  // 这里补上，否则 `asset.media_id` 是 undefined，点「设为主图」会提交空 body → 422。
  return raw.map((asset) => ({ ...asset, media_id: asset.media_id ?? asset.id ?? '' }))
}

export async function fetchStyles(projectId: string): Promise<StyleAsset[]> {
  return asList<StyleAsset>(await stylesApi.list(projectId))
}

/** 角色资产类型（API.md §5 generate 的 `asset_kinds`）。 */
export const ASSET_KIND_LABEL: Record<string, string> = {
  setting: '设定图',
  front: '正面',
  half_body: '半身',
  expression: '表情',
}

export const ASSET_KINDS = ['setting', 'front', 'half_body', 'expression'] as const
