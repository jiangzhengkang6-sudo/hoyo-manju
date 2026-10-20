import type { ApiErrorBody, ErrorCode, FieldIssue } from '@/types'

/** 业务错误码 → 中文短标题，用于 toast 与错误面板。 */
const CODE_TITLE: Record<string, string> = {
  VALIDATION_ERROR: '请求参数不合法',
  NOT_FOUND: '资源不存在',
  CONFLICT: '冲突',
  STATE_CONFLICT: '当前状态不允许该操作',
  IDEMPOTENCY_REPLAY: '重复提交',
  PROVIDER_ERROR: '供应商返回错误',
  PROVIDER_TIMEOUT: '供应商超时',
  SCHEMA_VALIDATION_FAILED: '剧本校验失败',
  SAFETY_BLOCKED: '内容安全检查未通过',
  QUOTA_EXCEEDED: '配额不足',
  STORAGE_ERROR: '存储错误',
  RENDER_ERROR: '合成失败',
  CHECKPOINT_STALE: '检查点已过期，请刷新后重试',
  INTERNAL_ERROR: '服务内部错误',
  NETWORK_ERROR: '无法连接后端服务',
  CLIENT_ERROR: '请求失败',
}

export function errorTitle(code: string): string {
  return CODE_TITLE[code] ?? code
}

/** 把 `["shots", 2, "duration_ms"]` 转成 `shots.2.duration_ms`。 */
export function locToPath(loc?: (string | number)[] | null): string {
  if (!loc || !loc.length) return ''
  return loc
    .map((seg) => (typeof seg === 'number' ? String(seg) : String(seg)))
    .join('.')
}

/** 把 `["shots", 2, "duration_ms"]` 转成人能读的定位文案。 */
export function locToLabel(loc?: (string | number)[] | null): string {
  if (!loc || !loc.length) return '整体'
  const parts: string[] = []
  for (let i = 0; i < loc.length; i += 1) {
    const seg = loc[i]
    const next = loc[i + 1]
    if (typeof seg === 'number') {
      // 数字下标：结合前一段语义化
      const owner = String(loc[i - 1] ?? '')
      if (owner === 'shots') parts.push(`第 ${seg + 1} 镜`)
      else if (owner === 'dialogue') parts.push(`第 ${seg + 1} 句台词`)
      else if (owner === 'characters') parts.push(`第 ${seg + 1} 个角色`)
      else if (owner === 'scenes') parts.push(`第 ${seg + 1} 场`)
      else if (owner === 'beat_sheet') parts.push(`第 ${seg + 1} 个节拍`)
      else if (owner === 'relationships') parts.push(`第 ${seg + 1} 条关系`)
      else parts.push(`#${seg}`)
      if (typeof next === 'string' && FIELD_LABEL[next]) parts.push(FIELD_LABEL[next])
      // 跳过对下一段字符串字段的重复输出
      if (typeof next === 'string' && FIELD_LABEL[next]) i += 1
      continue
    }
    const label = FIELD_LABEL[seg]
    if (label) parts.push(label)
  }
  return parts.length ? parts.join(' · ') : loc.join('.')
}

const FIELD_LABEL: Record<string, string> = {
  story_bible: '故事圣经',
  logline: '一句话梗概',
  theme: '主题',
  tone: '基调',
  setting: '世界观设定',
  characters: '角色',
  scenes: '场景',
  conflict: '冲突',
  relationships: '人物关系',
  beat_sheet: '节拍表',
  shots: '分镜',
  shot_no: '镜号',
  duration_ms: '时长',
  scene_no: '场景号',
  action: '画面动作',
  camera_movement: '镜头运动',
  composition: '构图',
  emotion: '情绪',
  dialogue: '台词',
  narration: '旁白',
  sfx: '音效',
  image_prompt: '画面提示词',
  negative_prompt: '负面提示词',
  video_params: '视频参数',
  name: '名称',
  identity: '身份',
  age_range: '年龄段',
  appearance: '外貌',
  outfit: '服饰',
  personality: '性格',
  speech_style: '说话风格',
  forbidden_changes: '禁用变化',
  text: '文本',
  character: '角色',
  goal: '目标',
  obstacle: '阻碍',
  stakes: '代价',
  beat_no: '节拍号',
  summary: '摘要',
  location: '地点',
  time_of_day: '时间',
  from: '起点',
  to: '终点',
  relation: '关系',
  required: '必需镜头',
}

/** 归一化后的 API 错误对象。 */
export class ApiError extends Error {
  readonly code: ErrorCode | string
  readonly status: number
  readonly details: Record<string, unknown> | null
  readonly retryable: boolean
  readonly traceId: string | null
  /** 字段级错误，已从 details.loc 映射出来。 */
  readonly issues: FieldIssue[]
  readonly title: string

  constructor(init: {
    code: string
    message: string
    status: number
    details?: Record<string, unknown> | null
    retryable?: boolean
    traceId?: string | null
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.status = init.status
    this.details = init.details ?? null
    this.retryable = init.retryable ?? false
    this.traceId = init.traceId ?? null
    this.title = errorTitle(init.code)
    this.issues = extractIssues(init.details)
  }

  /** 某个字段路径上的错误消息。 */
  issueFor(path: string): FieldIssue | undefined {
    return this.issues.find((i) => i.path === path || i.path.endsWith(`.${path}`))
  }

  hasIssue(path: string): boolean {
    return this.issueFor(path) !== undefined
  }
}

/** 从 details 中抽取字段级错误，兼容 loc / errors 两种形态。 */
export function extractIssues(details: Record<string, unknown> | null | undefined): FieldIssue[] {
  if (!details) return []
  const out: FieldIssue[] = []
  const pushLoc = (loc: unknown, message: unknown) => {
    const arr = Array.isArray(loc) ? (loc as (string | number)[]) : null
    out.push({
      path: locToPath(arr),
      label: locToLabel(arr),
      message: typeof message === 'string' ? message : '字段不合法',
      loc: arr ?? undefined,
    })
  }
  if (Array.isArray(details.loc)) {
    pushLoc(details.loc, typeof details.msg === 'string' ? details.msg : details.message)
  }
  if (Array.isArray(details.errors)) {
    for (const raw of details.errors) {
      if (raw && typeof raw === 'object') {
        const rec = raw as Record<string, unknown>
        pushLoc(rec.loc, rec.msg ?? rec.message)
      }
    }
  }
  if (Array.isArray(details.warnings)) {
    for (const raw of details.warnings) {
      if (raw && typeof raw === 'object') {
        const rec = raw as Record<string, unknown>
        pushLoc(rec.loc, rec.msg ?? rec.message)
      }
    }
  }
  return out
}

/** 解析后端统一错误体，非标准响应也能兜底。 */
export async function parseErrorResponse(res: Response): Promise<ApiError> {
  let body: unknown = null
  const raw = await res.text().catch(() => '')
  if (raw) {
    try {
      body = JSON.parse(raw)
    } catch {
      body = null
    }
  }
  const envelope = (body as { error?: ApiErrorBody } | null)?.error
  if (envelope && typeof envelope === 'object') {
    return new ApiError({
      code: String(envelope.code ?? 'CLIENT_ERROR'),
      message: String(envelope.message ?? `请求失败（HTTP ${res.status}）`),
      status: res.status,
      details: (envelope.details as Record<string, unknown> | null) ?? null,
      retryable: Boolean(envelope.retryable),
      traceId: envelope.trace_id ?? null,
    })
  }
  // 非统一错误体：把 HTTP 语义翻译成错误码，避免前端到处判断 status
  const fallbackCode =
    res.status === 404
      ? 'NOT_FOUND'
      : res.status === 409
        ? 'CONFLICT'
        : res.status === 422
          ? 'VALIDATION_ERROR'
          : res.status === 429
            ? 'QUOTA_EXCEEDED'
            : res.status >= 500
              ? 'INTERNAL_ERROR'
              : 'CLIENT_ERROR'
  const text = raw.slice(0, 300).trim()
  return new ApiError({
    code: fallbackCode,
    message: text || `请求失败（HTTP ${res.status}）`,
    status: res.status,
    retryable: res.status >= 500 || res.status === 429,
  })
}

export function networkError(err: unknown): ApiError {
  const message =
    err instanceof Error && err.message
      ? `无法连接后端服务：${err.message}`
      : '无法连接后端服务，请确认 FastAPI 已启动（后端默认端口 18437）'
  return new ApiError({ code: 'NETWORK_ERROR', message, status: 0, retryable: true })
}

/**
 * 把任意异常归一成 `ApiError`。
 *
 * **页面 catch 里必须用它，不要写 `err instanceof ApiError ? err : null`。**
 * 后者会把「请求失败」变成 `null`，页面随即渲染成**空态**（「暂无数据」/「还没有内容」），
 * 用户看到的是「你没有数据」而不是「加载失败，可重试」—— 真出故障时最误导人。
 * `http` 层已保证抛 `ApiError`，所以剩下的未知异常都是代码缺陷或非预期错误；
 * 归成 `NETWORK_ERROR` 至少能让用户看到失败与重试入口。
 */
export function toApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : networkError(err)
}

/** 幂等键：任务类提交必须携带，避免重复扣配额。 */
export function makeIdempotencyKey(scope: string): string {
  const rand =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : Math.random().toString(36).slice(2, 18)
  return `${scope}_${Date.now().toString(36)}_${rand}`
}
