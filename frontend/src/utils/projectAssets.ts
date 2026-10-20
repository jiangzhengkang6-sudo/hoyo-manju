/**
 * 项目级「角色与画风资产库」（PRD §5.1-6）的纯逻辑。
 *
 * 刻意与视图分离：过滤、统计、表单校验都是纯函数，可以直接单测，
 * 不必为了验证「搜索是否大小写不敏感」去挂载整个页面。
 * 视图里只保留 DOM 与请求编排。
 */
import { normalizePalette } from '@/api/workspace'
import type { Character, ProjectAssetCounts, StyleAsset, Voice } from '@/types'

/** 资产库的分段筛选值；空串表示「全部」。 */
export type ProjectAssetKind = '' | 'character' | 'style'

/** 把逗号/分号/空白分隔的色板输入解析成数组（中英文标点都接受）。 */
export function parsePaletteInput(text: string): string[] {
  return (text ?? '')
    .split(/[,，;；\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** 色板数组还原成可编辑的文本。 */
export function formatPaletteInput(palette: unknown): string {
  return normalizePalette(palette).join(', ')
}

function match(text: unknown, needle: string): boolean {
  return String(text ?? '').toLowerCase().includes(needle)
}

/** 角色过滤：名称 / 身份 / 外貌任一命中即可（用户不一定记得名字）。 */
export function filterCharacters(characters: Character[], query: string): Character[] {
  const q = (query ?? '').trim().toLowerCase()
  if (!q) return characters
  return characters.filter(
    (c) => match(c.name, q) || match(c.identity, q) || match(c.appearance, q),
  )
}

/** 画风过滤：名称 / 描述 / 色板任一命中。 */
export function filterStyles(styles: StyleAsset[], query: string): StyleAsset[] {
  const q = (query ?? '').trim().toLowerCase()
  if (!q) return styles
  return styles.filter(
    (s) =>
      match(s.name, q) ||
      match(s.description, q) ||
      normalizePalette(s.palette).some((v) => match(v, q)),
  )
}

/** 概览计数：既算总量，也算「有多少角色真的可用」（有参考图 / 有音色）。 */
export function assetCounts(
  characters: Character[],
  styles: StyleAsset[],
  defaultStyleId?: string | null,
): ProjectAssetCounts {
  return {
    characters: characters.length,
    styles: styles.length,
    withReference: characters.filter((c) => Boolean(c.primary_media_id)).length,
    withVoice: characters.filter((c) => Boolean(c.voice_id)).length,
    defaultStyleId: defaultStyleId ?? null,
  }
}

/** 当前分段下应该展示的条数，用于空态判定。 */
export function visibleCount(
  kind: ProjectAssetKind,
  characters: Character[],
  styles: StyleAsset[],
): number {
  if (kind === 'character') return characters.length
  if (kind === 'style') return styles.length
  return characters.length + styles.length
}

export function showCharacters(kind: ProjectAssetKind): boolean {
  return kind !== 'style'
}

export function showStyles(kind: ProjectAssetKind): boolean {
  return kind !== 'character'
}

/** 新建角色的必填校验；返回 null 表示通过。 */
export function validateCharacterForm(name: string): string | null {
  const trimmed = (name ?? '').trim()
  if (!trimmed) return '角色名不能为空'
  if (trimmed.length > 60) return '角色名不能超过 60 个字'
  return null
}

/** 新建/编辑画风的必填校验。 */
export function validateStyleForm(name: string): string | null {
  const trimmed = (name ?? '').trim()
  if (!trimmed) return '画风名不能为空'
  if (trimmed.length > 120) return '画风名不能超过 120 个字'
  return null
}

/** 音色展示：绑定了就显示音色名，没绑定明确说「未绑定音色」（FR-050 的前置条件）。 */
export function voiceLabel(character: Character, voices: Voice[]): string {
  if (!character.voice_id) return '未绑定音色'
  const hit = voices.find((v) => v.id === character.voice_id)
  return hit?.name ? `音色 ${hit.name}` : `音色 ${character.voice_id}`
}

/** FR-051 语速展示；缺省按 1.0（后端默认值）显示，避免出现空白。 */
export function speedLabel(character: Character): string {
  const speed = typeof character.voice_speed === 'number' ? character.voice_speed : 1
  return `语速 ${speed.toFixed(2)}×`
}

/** 角色是否「可用」：有主参考图且已确认。用于给出可执行的下一步提示。 */
export function characterReadiness(character: Character): {
  ready: boolean
  missing: string[]
} {
  const missing: string[] = []
  if (!character.primary_media_id) missing.push('未确认参考图')
  if (!character.voice_id) missing.push('未绑定音色')
  return { ready: missing.length === 0, missing }
}
