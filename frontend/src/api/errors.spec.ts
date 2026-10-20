/**
 * 统一错误体契约（docs/API.md §0.1）在前端侧的回归。
 *
 * 重点：后端每个错误路径都必须带 `trace_id`，前端必须把它取到并透出 ——
 * 否则用户报障时我们拿不到可检索的请求标识。
 */
import { describe, expect, it } from 'vitest'

import {
  ApiError,
  errorTitle,
  extractIssues,
  locToLabel,
  locToPath,
  makeIdempotencyKey,
  networkError,
  toApiError,
  parseErrorResponse,
} from '@/api/errors'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('统一错误体解析', () => {
  it('标准错误体：字段与 trace_id 全部取到', async () => {
    const err = await parseErrorResponse(
      jsonResponse(422, {
        error: {
          code: 'VALIDATION_ERROR',
          message: 'body.name：Field required',
          details: { loc: ['body', 'name'], msg: 'Field required' },
          retryable: false,
          trace_id: '11eb86dc1bf4',
        },
      }),
    )

    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('VALIDATION_ERROR')
    expect(err.status).toBe(422)
    expect(err.message).toBe('body.name：Field required')
    expect(err.retryable).toBe(false)
    expect(err.traceId).toBe('11eb86dc1bf4')
    expect(err.title).toBe('请求参数不合法')
  })

  it('retryable=true 被如实透出（供应商超时等）', async () => {
    const err = await parseErrorResponse(
      jsonResponse(504, {
        error: {
          code: 'PROVIDER_TIMEOUT',
          message: '供应商超时',
          retryable: true,
          trace_id: 'abc123',
        },
      }),
    )
    expect(err.retryable).toBe(true)
    expect(err.title).toBe('供应商超时')
  })

  it('缺少 trace_id 时不崩溃，置为 null', async () => {
    const err = await parseErrorResponse(
      jsonResponse(500, { error: { code: 'INTERNAL_ERROR', message: '内部错误' } }),
    )
    expect(err.traceId).toBeNull()
    expect(err.retryable).toBe(false)
  })

  it('能力探测失败时 details.problems 原样保留', async () => {
    const problems = [{ field: 'duration_ms', requested: 12000, message: '超出上限' }]
    const err = await parseErrorResponse(
      jsonResponse(422, {
        error: {
          code: 'PROVIDER_CAPABILITY_UNSUPPORTED',
          message: '当前供应商不支持该参数',
          details: { problems },
          retryable: false,
          trace_id: 'cap01',
        },
      }),
    )
    expect(err.details?.problems).toEqual(problems)
  })

  it('非统一错误体（FastAPI 默认 detail）按 HTTP 语义翻译', async () => {
    const err = await parseErrorResponse(jsonResponse(404, { detail: 'Not Found' }))
    expect(err.code).toBe('NOT_FOUND')
    expect(err.status).toBe(404)
    expect(err.traceId).toBeNull()
  })

  it('5xx / 429 在兜底路径上被标记为可重试', async () => {
    const five = await parseErrorResponse(new Response('boom', { status: 503 }))
    expect(five.code).toBe('INTERNAL_ERROR')
    expect(five.retryable).toBe(true)

    const quota = await parseErrorResponse(new Response('slow down', { status: 429 }))
    expect(quota.code).toBe('QUOTA_EXCEEDED')
    expect(quota.retryable).toBe(true)
  })

  it('响应体不是 JSON 时用文本兜底且不抛异常', async () => {
    const err = await parseErrorResponse(new Response('<html>500</html>', { status: 500 }))
    expect(err.status).toBe(500)
    expect(err.message).toContain('500')
    expect(err.retryable).toBe(true)
  })

  it('空响应体也能给出可读消息', async () => {
    const err = await parseErrorResponse(new Response('', { status: 400 }))
    expect(err.message).toContain('400')
    expect(err.code).toBe('CLIENT_ERROR')
  })
})

describe('字段级错误抽取', () => {
  it('details.loc + msg 抽成一条 issue', () => {
    const issues = extractIssues({ loc: ['body', 'name'], msg: 'Field required' })
    expect(issues).toHaveLength(1)
    expect(issues[0].path).toBe('body.name')
    expect(issues[0].message).toBe('Field required')
  })

  it('details.errors[] 批量抽取（剧本 schema 校验）', () => {
    const issues = extractIssues({
      errors: [
        { loc: ['shots', 2, 'duration_ms'], msg: '时长超出范围' },
        { loc: ['shots', 0, 'action'], msg: '不能为空' },
      ],
    })
    expect(issues).toHaveLength(2)
    expect(issues[0].path).toBe('shots.2.duration_ms')
    // 先点名容器（分镜），再给下标与字段 —— 比裸下标可读
    expect(issues[0].label).toBe('分镜 · 第 3 镜 · 时长')
    expect(issues[1].label).toBe('分镜 · 第 1 镜 · 画面动作')
  })

  it('issueFor 支持按尾段匹配，便于表单逐字段标红', () => {
    const err = new ApiError({
      code: 'SCHEMA_VALIDATION_FAILED',
      message: 'x',
      status: 422,
      details: { errors: [{ loc: ['shots', 1, 'image_prompt'], msg: '提示词过长' }] },
    })
    expect(err.hasIssue('image_prompt')).toBe(true)
    expect(err.issueFor('image_prompt')?.message).toBe('提示词过长')
    expect(err.hasIssue('不存在字段')).toBe(false)
  })

  it('warnings 也会被抽出（不阻断但需提示）', () => {
    const issues = extractIssues({ warnings: [{ loc: ['shots', 0, 'sfx'], msg: '音效建议补充' }] })
    expect(issues).toHaveLength(1)
    expect(issues[0].message).toBe('音效建议补充')
  })

  it('details 为空时返回空数组', () => {
    expect(extractIssues(null)).toEqual([])
    expect(extractIssues({})).toEqual([])
  })
})

describe('loc 文案', () => {
  it('locToPath 用点号连接', () => {
    expect(locToPath(['shots', 2, 'duration_ms'])).toBe('shots.2.duration_ms')
    expect(locToPath([])).toBe('')
    expect(locToPath(null)).toBe('')
  })

  it('locToLabel 把下标转成人话且不重复输出字段名', () => {
    expect(locToLabel(['shots', 2, 'duration_ms'])).toBe('分镜 · 第 3 镜 · 时长')
    expect(locToLabel(['shots', 0, 'dialogue', 1, 'text'])).toBe(
      '分镜 · 第 1 镜 · 台词 · 第 2 句台词 · 文本',
    )
    expect(locToLabel(['beat_sheet', 0, 'summary'])).toBe('节拍表 · 第 1 个节拍 · 摘要')
    expect(locToLabel(['characters', 0, 'name'])).toBe('角色 · 第 1 个角色 · 名称')
  })

  it('locToLabel 空 loc 显示「整体」', () => {
    expect(locToLabel([])).toBe('整体')
    expect(locToLabel(null)).toBe('整体')
  })

  it('未知字段回落到原始点号路径，不吞掉信息', () => {
    expect(locToLabel(['未知段', '另一段'])).toBe('未知段.另一段')
  })
})

describe('错误码标题', () => {
  it('已知码有中文标题，未知码原样返回', () => {
    expect(errorTitle('QUOTA_EXCEEDED')).toBe('配额不足')
    expect(errorTitle('SOME_NEW_CODE')).toBe('SOME_NEW_CODE')
  })
})

describe('网络错误与幂等键', () => {
  it('networkError 可重试且状态码为 0', () => {
    const err = networkError(new TypeError('Failed to fetch'))
    expect(err.code).toBe('NETWORK_ERROR')
    expect(err.status).toBe(0)
    expect(err.retryable).toBe(true)
    expect(err.message).toContain('Failed to fetch')
  })

  it('makeIdempotencyKey 带作用域前缀且多次调用不重复', () => {
    const a = makeIdempotencyKey('gen_images')
    const b = makeIdempotencyKey('gen_images')
    expect(a.startsWith('gen_images_')).toBe(true)
    expect(a).not.toBe(b)
  })
})

describe('toApiError：页面 catch 的唯一归一化入口', () => {
  it('已是 ApiError 时原样返回（保留 message / status / trace_id / details）', () => {
    const original = new ApiError({
      code: 'SAFETY_BLOCKED',
      message: '本集存在未通过内容安全检查的素材',
      status: 422,
      details: { blocked: ['med_1'] },
      traceId: 'tr_abc',
    })
    const result = toApiError(original)
    expect(result).toBe(original)
    expect(result.traceId).toBe('tr_abc')
    expect(result.details).toEqual({ blocked: ['med_1'] })
  })

  it('未知异常归成 NETWORK_ERROR，而不是 null', () => {
    // 关键：返回 null 会让页面把「加载失败」渲染成空态（「暂无数据」），
    // 用户看到的是「你没有数据」而不是「失败了，可重试」。
    const result = toApiError(new TypeError('Cannot read properties of undefined'))
    expect(result).toBeInstanceOf(ApiError)
    expect(result.code).toBe('NETWORK_ERROR')
    expect(result.status).toBe(0)
    expect(result.retryable).toBe(true)
  })

  it('非 Error 的抛出物（字符串 / 数字 / null）也能归一化', () => {
    expect(toApiError('boom').code).toBe('NETWORK_ERROR')
    expect(toApiError(42).code).toBe('NETWORK_ERROR')
    expect(toApiError(null).code).toBe('NETWORK_ERROR')
  })

  it('永不返回 null —— 视图层 loadError 依赖这一点', () => {
    for (const thrown of [new Error('x'), 'x', 42, null, undefined, {}]) {
      expect(toApiError(thrown)).toBeInstanceOf(ApiError)
    }
  })
})
