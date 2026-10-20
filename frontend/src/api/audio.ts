import { http, requestText } from './client'
import { asList } from './projects'
import type {
  AudioTrack,
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

  /** 试听地址（直接给 <audio src>）。 */
  voicePreviewUrl: (voiceId: string) => `/voices/${voiceId}/preview`,

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

  /** §12-4：用上传的音频顶替某句台词（TTS 失败时的兜底）。 */
  ttsManual: (episodeId: string, body: { shot_no: number; line_index: number; media_id: string }) =>
    http.post<TtsLine>(`/episodes/${episodeId}/tts/manual`, body),

  /** §12-4：跳过这些镜头的配音，继续预览/导出。 */
  ttsSkip: (episodeId: string, body: { shot_nos: number[]; reason?: string }) =>
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
