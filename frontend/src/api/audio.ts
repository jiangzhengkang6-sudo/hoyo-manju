import { apiUrl } from './base'
import { http, requestText } from './client'
import { asList } from './projects'
import type {
  AudioTrack,
  BgmEstimate,
  BgmSuggestion,
  Job,
  SubtitleCue,
  TrackKind,
  TtsEstimate,
  TtsLine,
  TtsLineStatus,
  Voice,
} from '@/types'

/** 音频与字幕（API.md §8）。 */
export const audioApi = {
  /* ---- 音色 ---- */

  voices: (projectId: string) =>
    http.get<Voice[] | { items: Voice[] }>(`/projects/${projectId}/voices`),

  /** 新增音色：上传或供应商音色。 */
  createVoice: (projectId: string, body: Partial<Voice> & { media_id?: string }) =>
    http.post<Voice>(`/projects/${projectId}/voices`, body),

  /** 试听地址（直接给 <audio src>，必须带 /api/v1 前缀）。 */
  voicePreviewUrl: (voiceId: string) => apiUrl(`/voices/${voiceId}/preview`),

  /**
   * FR-050「绑定前可试听」：后端按需合成并缓存试听音频，返回可播放地址。
   * 首次调用可能较慢（要走一次供应商合成）。
   */
  previewVoice: (voiceId: string) =>
    http.get<{ media_id: string; url: string }>(`/voices/${voiceId}/preview`),

  /* ---- TTS ---- */

  ttsGenerate: (episodeId: string, body: { shot_nos?: number[]; character_id?: string }) =>
    http.post<Job>(`/episodes/${episodeId}/tts/generate`, body),

  /**
   * FR-050：TTS 前置条件预估。
   * `can_generate=false` 时必须先绑定音色，否则提交会被 409 拒绝。
   */
  ttsEstimate: (episodeId: string, params: { shot_nos?: string; character_id?: string } = {}) =>
    http.get<TtsEstimate>(`/episodes/${episodeId}/tts/estimate`, params),

  ttsList: (episodeId: string) =>
    http.get<TtsLine[] | { items: TtsLine[] }>(`/episodes/${episodeId}/tts`),

  /**
   * §12-4：逐句台词状态表（含还没生成音频的行）。
   * 「上传音频 / 跳过」入口只应出现在 `status === 'missing'` 的行上。
   */
  ttsLines: (episodeId: string) =>
    http.get<{ items: TtsLineStatus[]; counts: Record<string, number> }>(
      `/episodes/${episodeId}/tts/lines`,
    ),

  /** 调整**已生成**音频的语速（本地变速，不调用模型、不计费）。 */
  ttsSpeed: (episodeId: string, trackId: string, speed: number) =>
    http.post<TtsLine>(`/episodes/${episodeId}/tts/${trackId}/speed`, { speed }),

  /** §12-4：用上传的音频顶替某句台词（TTS 失败时的兜底）。 */
  ttsManual: (episodeId: string, body: { shot_no: number; line_index: number; media_id: string }) =>
    http.post<TtsLine>(`/episodes/${episodeId}/tts/manual`, body),

  /** §12-4：跳过配音；带 line_index 时只跳过该镜的那一句。 */
  ttsSkip: (episodeId: string, body: { shot_nos: number[]; reason?: string; line_index?: number }) =>
    http.post<{ skipped_track_ids: string[]; counts: Record<string, number> }>(
      `/episodes/${episodeId}/tts/skip`,
      body,
    ),

  /* ---- BGM / 音效 ---- */

  bgmList: (episodeId: string) =>
    http.get<AudioTrack[] | { items: AudioTrack[] }>(`/episodes/${episodeId}/bgm`),

  bgmCreate: (episodeId: string, body: Omit<AudioTrack, 'id'>) =>
    http.post<AudioTrack>(`/episodes/${episodeId}/bgm`, body),

  bgmDelete: (episodeId: string, trackId: string) =>
    http.del<void>(`/episodes/${episodeId}/bgm/${trackId}`),

  /**
   * 氛围音 / BGM 的成本预估（按生成秒数计费）。
   * 传镜头参数时以这段镜头在时间线上的实际长度为准（可跨多个镜头共用）。
   */
  bgmEstimate: (
    episodeId: string,
    options: {
      seconds?: number
      shot_no?: number
      shot_from?: number
      shot_to?: number
    } = {},
  ) =>
    http.get<BgmEstimate>(`/episodes/${episodeId}/bgm/estimate`, {
      ...(options.seconds ? { seconds: options.seconds } : {}),
      ...(options.shot_no ? { shot_no: options.shot_no } : {}),
      ...(options.shot_from ? { shot_from: options.shot_from } : {}),
      ...(options.shot_to ? { shot_to: options.shot_to } : {}),
    }),

  /**
   * 按镜头内容生成氛围描述草稿（读分镜的场景/音效/情绪，不调模型、不扣费）。
   */
  bgmSuggest: (episodeId: string, shotFrom: number, shotTo?: number) =>
    http.get<BgmSuggestion>(`/episodes/${episodeId}/bgm/suggest`, {
      shot_from: shotFrom,
      ...(shotTo ? { shot_to: shotTo } : {}),
    }),

  /**
   * 生成一轨氛围音 / BGM（异步任务，成功后自动挂到时间线）。
   * 不传镜头 = 整集垫乐；传 `shot_from/shot_to` = 覆盖这段镜头（长度按镜头裁）。
   */
  bgmGenerate: (
    episodeId: string,
    body: {
      prompt: string
      seconds?: number
      shot_no?: number
      shot_from?: number
      shot_to?: number
      volume_db?: number
      fade_in_ms?: number
      fade_out_ms?: number
    },
  ) => http.post<Job>(`/episodes/${episodeId}/bgm/generate`, body),

  /* ---- 字幕 ---- */

  subtitlesGenerate: (episodeId: string) =>
    http.post<Job>(`/episodes/${episodeId}/subtitles/generate`),

  subtitles: (episodeId: string) =>
    http.get<SubtitleCue[] | { items: SubtitleCue[] }>(`/episodes/${episodeId}/subtitles`),

  /** 纯文本下载（SRT / VTT）。 */
  subtitlesText: (episodeId: string, format: 'srt' | 'vtt') =>
    requestText(`/episodes/${episodeId}/subtitles/export`, { format }),

  /**
   * FR-053：人工校正单条字幕。
   * 只提交要改的字段，未提交的保持原值；服务端会重新校验时间区间。
   */
  subtitleUpdate: (
    episodeId: string,
    cueId: string,
    body: { text?: string; start_ms?: number; end_ms?: number },
  ) => http.patch<SubtitleCue>(`/episodes/${episodeId}/subtitles/${cueId}`, body),
}

export async function fetchVoices(projectId: string): Promise<Voice[]> {
  return asList<Voice>(await audioApi.voices(projectId))
}

export async function fetchTtsLines(episodeId: string): Promise<TtsLine[]> {
  const lines = asList<TtsLine>(await audioApi.ttsList(episodeId))
  return [...lines].sort((a, b) => (a.shot_no ?? 0) - (b.shot_no ?? 0))
}

export async function fetchTracks(episodeId: string): Promise<AudioTrack[]> {
  return asList<AudioTrack>(await audioApi.bgmList(episodeId))
}

export async function fetchSubtitles(episodeId: string): Promise<SubtitleCue[]> {
  const cues = asList<SubtitleCue>(await audioApi.subtitles(episodeId))
  return [...cues].sort((a, b) => (a.start_ms ?? 0) - (b.start_ms ?? 0))
}

export const TRACK_KIND_LABEL: Record<TrackKind, string> = {
  bgm: '背景音乐',
  ambience: '环境音',
  sfx: '音效',
}
