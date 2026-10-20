import { http } from './client'
import { asList } from './projects'
import type {
  Character,
  CharacterAsset,
  CharacterGenerateInput,
  CharacterInput,
  Job,
  ReplacePrimaryResult,
  StyleApplyResult,
  StyleAsset,
} from '@/types'

/** 角色与画风（API.md §5）。 */
export const charactersApi = {
  list: (projectId: string) =>
    http.get<Character[] | { items: Character[] }>(`/projects/${projectId}/characters`),

  create: (projectId: string, body: CharacterInput) =>
    http.post<Character>(`/projects/${projectId}/characters`, body),

  get: (characterId: string) => http.get<Character>(`/characters/${characterId}`),

  patch: (characterId: string, body: Partial<CharacterInput>) =>
    http.patch<Character>(`/characters/${characterId}`, body),

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

export async function fetchCharacters(projectId: string): Promise<Character[]> {
  return asList<Character>(await charactersApi.list(projectId))
}

export async function fetchCharacterAssets(characterId: string): Promise<CharacterAsset[]> {
  return asList<CharacterAsset>(await charactersApi.assets(characterId))
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
