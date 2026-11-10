/** 展示层格式化工具。数值/时长一律使用等宽字体展示。 */

/** 毫秒 → `1:23.4` 或 `4.0s`。 */
export function formatDuration(ms?: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—'
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  const totalSec = ms / 1000
  const min = Math.floor(totalSec / 60)
  const sec = Math.floor(totalSec % 60)
  return `${min}:${String(sec).padStart(2, '0')}`
}

/** 秒 → `mm:ss`。 */
export function formatSeconds(sec?: number | null): string {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return '—'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

/** ISO 时间 → 本地 `MM-DD HH:mm`。 */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const d = new Date(t)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** ISO 时间 → 相对时间（刚刚 / 3 分钟前 / 2 天前）。 */
export function relativeTime(iso?: string | null, now = Date.now()): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diff = Math.max(0, now - t)
  const sec = Math.floor(diff / 1000)
  if (sec < 45) return '刚刚'
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return formatDateTime(iso)
}

/** 字节 → 可读体积。 */
export function formatBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/** 截断长文本。 */
export function truncate(text?: string | null, max = 60): string {
  if (!text) return ''
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** 安全 JSON 字符串化（缩进 2）。 */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/** 生成稳定的 CSS 宽度百分比（用于进度/成本条）。 */
export function percent(part?: number | null, total?: number | null): number {
  if (!part || !total || total <= 0) return 0
  return Math.max(0, Math.min(100, (part / total) * 100))
}

/** 供应商调用状态 → 中文。后端返回 SUCCEEDED 这类枚举，界面上不能直接铺。 */
const PROVIDER_STATUS_LABELS: Record<string, string> = {
  PENDING: '待提交',
  QUEUED: '排队中',
  RUNNING: '执行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELLED: '已取消',
  TIMEOUT: '超时',
}

export function providerStatusLabel(status?: string | null): string {
  if (!status) return '—'
  return PROVIDER_STATUS_LABELS[status.toUpperCase()] ?? '未知状态'
}
