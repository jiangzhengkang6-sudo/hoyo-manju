import { apiUrl } from './base'
import { http } from './client'
import { asList } from './projects'
import type { MediaAsset, MediaKind } from '@/types'

/** 媒体与上传（API.md §11）。 */
export const mediaApi = {
  /** multipart：`file` + `kind` + `rights_declared` + `source_note` + `episode_id`。 */
  upload: (
    file: File,
    kind: MediaKind,
    extra: { rights_declared?: boolean; source_note?: string; episode_id?: string } = {},
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('kind', kind)
    fd.append('rights_declared', String(extra.rights_declared ?? false))
    if (extra.source_note) fd.append('source_note', extra.source_note)
    if (extra.episode_id) fd.append('episode_id', extra.episode_id)
    return http.upload<MediaAsset>('/media/upload', fd)
  },

  get: (mediaId: string) => http.get<MediaAsset>(`/media/${mediaId}`),

  /** 签名 URL（默认 30 分钟有效）。 */
  signedUrl: (mediaId: string) => http.get<{ url: string }>(`/media/${mediaId}/url`),

  /** 本地开发直连内容地址，可直接作为 <img>/<video> 的 src。 */
  /**
   * 媒体内容地址（给 `<img src>` / `<video src>` 直接用）。
   *
   * 必须带 `/api/v1` 前缀：`http` 客户端会自动加，但直接塞给 DOM 的地址不会。
   * 少了前缀就会打到前端 dev server 的根路径，拿到 SPA 的 HTML → 视频黑屏/无法播放。
   */
  contentUrl: (mediaId: string) => apiUrl(`/media/${mediaId}/content`),

  episodeMedia: (episodeId: string, kind?: MediaKind) =>
    http.get<MediaAsset[] | { items: MediaAsset[] }>(`/episodes/${episodeId}/media`, { kind }),

  rights: (mediaId: string) => http.get<Record<string, unknown>>(`/media/${mediaId}/rights`),

  putRights: (mediaId: string, body: Record<string, unknown>) =>
    http.put<Record<string, unknown>>(`/media/${mediaId}/rights`, body),
}

export async function fetchEpisodeMedia(episodeId: string, kind?: MediaKind): Promise<MediaAsset[]> {
  return asList<MediaAsset>(await mediaApi.episodeMedia(episodeId, kind))
}

/** 媒体资源显示地址：优先签名 URL，其次本地直连内容接口。 */
export function mediaSrc(mediaId?: string | null, url?: string | null): string | null {
  if (url) return url
  if (!mediaId) return null
  return mediaApi.contentUrl(mediaId)
}

export const MEDIA_KIND_LABEL: Record<string, string> = {
  text: '文本',
  image: '图片',
  audio: '音频',
  video: '视频',
}
