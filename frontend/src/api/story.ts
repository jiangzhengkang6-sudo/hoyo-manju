import { http } from './client'
import { asList } from './projects'
import type {
  Job,
  RegenerateShotsInput,
  ScriptDocument,
  ScriptValidationResult,
  StoryAnalysis,
  StoryDocument,
  StoryEstimate,
  StoryInputType,
  StoryJson,
  StoryVersion,
  VersionDiff,
  VersionRecord,
} from '@/types'

/**
 * 故事与剧本（API.md §4）。
 * 故事输入长度上限 2000 字（API.md §4 明示，非配置项）。
 */
export const STORY_TEXT_MAX = 2000

export const storyApi = {
  /** 保存故事输入；`input_type ∈ {idea, text, manual}`，长度 ≤ 2000 字。 */
  save: (episodeId: string, body: { input_type: StoryInputType; source_text: string }) =>
    http.post<StoryDocument>(`/episodes/${episodeId}/story`, body),

  get: (episodeId: string) => http.get<StoryDocument>(`/episodes/${episodeId}/story`),

  versions: (episodeId: string) =>
    http.get<StoryVersion[] | { items: StoryVersion[] }>(`/episodes/${episodeId}/story/versions`),

  /**
   * FR-010：长文本入口的字符数与预计生成成本。
   * 后端与 `/story/generate` 共用同一份提示词，因此预估与实扣口径一致。
   */
  estimate: (episodeId: string, body: { source_text: string; shot_count?: number }) =>
    http.post<StoryEstimate>(`/episodes/${episodeId}/story/estimate`, body),

  /** 触发 StoryAgent（异步 job，type `STORY_GENERATION`）。 */
  generate: (episodeId: string) =>
    http.post<Job>(`/episodes/${episodeId}/story/generate`),

  /**
   * 故事分析（同步接口，结果 `{analysis, story}`）。
   * 后端在模型不可用时会用已有剧本兜底派生，因此总是有结构化结果。
   */
  analyze: (episodeId: string) =>
    http.post<{ analysis: StoryAnalysis; story: StoryDocument | null }>(
      `/episodes/${episodeId}/story/analyze`,
    ),
}

export const scriptApi = {
  get: (episodeId: string) => http.get<ScriptDocument>(`/episodes/${episodeId}/script`),

  /** 保存人工编辑，`base_version` 不一致会返回 CHECKPOINT_STALE / CONFLICT。 */
  save: (episodeId: string, body: { story_json: StoryJson; base_version: number }) =>
    http.put<ScriptDocument>(`/episodes/${episodeId}/script`, body),

  validate: (episodeId: string, body: { story_json: StoryJson }) =>
    http.post<ScriptValidationResult>(`/episodes/${episodeId}/script/validate`, body),

  /** 局部重生成分镜。 */
  regenerateShots: (episodeId: string, body: RegenerateShotsInput) =>
    http.post<Job>(`/episodes/${episodeId}/shots/regenerate`, body),

  /**
   * FR-014：按剧情节拍局部重生成。服务端按 `beat_sheet[].shots` 换算镜号，
   * 未选中的节拍不会被改动。
   */
  regenerateBeats: (
    episodeId: string,
    body: { beat_nos: number[]; replan_downstream?: boolean; instruction?: string },
  ) => http.post<Job & { shot_nos: number[] }>(`/episodes/${episodeId}/beats/regenerate`, body),
}

/* ------------------------------------------------------------------ *
 * FR-004 版本记录
 * ------------------------------------------------------------------ */

/** 列出剧集的剧本/角色描述/分镜提示词/时间线版本。 */
export const versionsApi = {
  list: (
    episodeId: string,
    params: { entity_type?: string; entity_id?: string; limit?: number } = {},
  ) =>
    http.get<{ items: VersionRecord[]; types: Record<string, string> }>(
      `/episodes/${episodeId}/versions`,
      params,
    ),

  /** 读取某版本完整快照，并给出与上一版的字段差异。 */
  detail: (episodeId: string, versionId: string) =>
    http.get<VersionRecord & { payload?: Record<string, unknown>; diff: VersionDiff }>(
      `/episodes/${episodeId}/versions/${versionId}`,
    ),
}

export async function fetchStoryVersions(episodeId: string): Promise<StoryVersion[]> {
  return asList<StoryVersion>(await storyApi.versions(episodeId))
}

/* ------------------------------------------------------------------ *
 * 剧本枚举（API.md §4.1）
 * ------------------------------------------------------------------ */

export const CAMERA_MOVEMENTS = ['固定', '推', '拉', '摇', '移', '跟', '升降'] as const

export const COMPOSITIONS = ['特写', '近景', '中景', '全景', '远景', '过肩', '俯拍', '仰拍'] as const

export const STORY_INPUT_TYPE_LABEL: Record<string, string> = {
  idea: '一句话创意',
  text: '故事文本',
  manual: '手工录入',
}

/** 空剧本骨架：仅用于「手工录入」时给用户一个可编辑的起点。 */
export function emptyStoryJson(): StoryJson {
  return {
    story_bible: {
      logline: '',
      theme: '',
      tone: '',
      setting: '',
      characters: [],
      scenes: [],
      conflict: { goal: '', obstacle: '', stakes: '' },
      relationships: [],
    },
    beat_sheet: [],
    shots: [],
  }
}
