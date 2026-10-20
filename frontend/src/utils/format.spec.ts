/**
 * 展示层格式化回归。
 *
 * 金额一律以**最小货币单位整数（分）**传输（docs/API.md 约定），
 * 所以这里的重点是：整数分不能因浮点运算显示错位，
 * 以及「无数据」必须显示占位符而不是 0 / NaN。
 */
import { describe, expect, it } from 'vitest'

import { formatCents, formatCentsSigned, ledgerKindLabel, ledgerKindTone, stageCostLabel } from '@/api/cost'
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatSeconds,
  percent,
  prettyJson,
  relativeTime,
  truncate,
} from '@/utils/format'

describe('金额（整数分）', () => {
  it('分转元保留两位小数', () => {
    expect(formatCents(0)).toBe('¥0.00')
    expect(formatCents(1)).toBe('¥0.01')
    expect(formatCents(1054)).toBe('¥10.54')
    expect(formatCents(19999)).toBe('¥199.99')
    expect(formatCents(100000)).toBe('¥1000.00')
  })

  it('大额整数分不出现浮点误差', () => {
    // 0.1+0.2 那类问题在金额上不可接受
    expect(formatCents(2070)).toBe('¥20.70')
    expect(formatCents(1145)).toBe('¥11.45')
    expect(formatCents(33333333)).toBe('¥333333.33')
  })

  it('缺失值显示占位符，不显示 ¥0.00', () => {
    expect(formatCents(null)).toBe('—')
    expect(formatCents(undefined)).toBe('—')
  })

  it('带符号金额用正负号区分，0 不带号', () => {
    expect(formatCentsSigned(500)).toBe('+¥5.00')
    expect(formatCentsSigned(-500)).toBe('−¥5.00')
    expect(formatCentsSigned(0)).toBe('¥0.00')
    expect(formatCentsSigned(null)).toBe('—')
  })
})

describe('成本阶段与流水类型文案', () => {
  it('已知阶段有中文名，未知阶段原样返回', () => {
    expect(stageCostLabel('image')).toBe('分镜图')
    expect(stageCostLabel('tts')).toBe('配音')
    expect(stageCostLabel('unknown_stage')).toBe('unknown_stage')
    expect(stageCostLabel(null)).toBe('未分类')
  })

  it('流水三种类型（RESERVE/SETTLE/RELEASE）都有中文名与色调', () => {
    expect(ledgerKindLabel('RESERVE')).toBe('预留')
    expect(ledgerKindLabel('SETTLE')).toBe('结算')
    expect(ledgerKindLabel('RELEASE')).toBe('释放')
    expect(ledgerKindLabel(null)).toBe('未知')

    expect(ledgerKindTone('RESERVE')).toBe('warn')
    expect(ledgerKindTone('SETTLE')).toBe('danger')
    expect(ledgerKindTone('RELEASE')).toBe('ok')
    expect(ledgerKindTone('NOPE')).toBe('info')
  })
})

describe('时长格式化', () => {
  it('小于一分钟用秒并保留一位小数', () => {
    expect(formatDuration(0)).toBe('0.0s')
    expect(formatDuration(4000)).toBe('4.0s')
    expect(formatDuration(15500)).toBe('15.5s')
    expect(formatDuration(59900)).toBe('59.9s')
  })

  it('达到一分钟用 m:ss', () => {
    expect(formatDuration(60000)).toBe('1:00')
    expect(formatDuration(125000)).toBe('2:05')
    expect(formatDuration(3600000)).toBe('60:00')
  })

  it('缺失值显示占位符', () => {
    expect(formatDuration(null)).toBe('—')
    expect(formatDuration(undefined)).toBe('—')
    expect(formatDuration(Number.NaN)).toBe('—')
  })

  it('formatSeconds 输出 mm:ss 且补零', () => {
    expect(formatSeconds(0)).toBe('0:00')
    expect(formatSeconds(65)).toBe('1:05')
    expect(formatSeconds(600)).toBe('10:00')
    expect(formatSeconds(null)).toBe('—')
  })
})

describe('时间与相对时间', () => {
  it('非法 ISO 原样返回而不是 Invalid Date', () => {
    expect(formatDateTime('不是时间')).toBe('不是时间')
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime('')).toBe('—')
  })

  it('相对时间分档：刚刚 / 分钟 / 小时 / 天', () => {
    const base = Date.parse('2026-10-13T12:00:00Z')
    expect(relativeTime(new Date(base - 10_000).toISOString(), base)).toBe('刚刚')
    expect(relativeTime(new Date(base - 5 * 60_000).toISOString(), base)).toBe('5 分钟前')
    expect(relativeTime(new Date(base - 3 * 3600_000).toISOString(), base)).toBe('3 小时前')
    expect(relativeTime(new Date(base - 2 * 86400_000).toISOString(), base)).toBe('2 天前')
  })

  it('相对时间不会出现负数（时钟偏移时按「刚刚」处理）', () => {
    const base = Date.parse('2026-10-13T12:00:00Z')
    expect(relativeTime(new Date(base + 60_000).toISOString(), base)).toBe('刚刚')
  })

  it('超过 30 天回落到绝对时间', () => {
    const base = Date.parse('2026-10-13T12:00:00Z')
    const old = new Date(base - 40 * 86400_000).toISOString()
    expect(relativeTime(old, base)).toBe(formatDateTime(old))
  })
})

describe('体积与文本', () => {
  it('字节按 1024 进制换算，B 不带小数', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(1024 * 1024)).toBe('1.0 MB')
    expect(formatBytes(4680 * 1024)).toBe('4.6 MB')
  })

  it('缺失体积显示占位符', () => {
    expect(formatBytes(null)).toBe('—')
    expect(formatBytes(undefined)).toBe('—')
  })

  it('truncate 超长才截断并加省略号', () => {
    expect(truncate('短文本', 10)).toBe('短文本')
    expect(truncate('12345678901', 10)).toBe('1234567890…')
    expect(truncate('', 10)).toBe('')
    expect(truncate(null, 10)).toBe('')
  })

  it('prettyJson 输出缩进 JSON，循环引用不抛异常', () => {
    expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}')
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(() => prettyJson(circular)).not.toThrow()
  })
})

describe('百分比（进度条）', () => {
  it('正常计算并夹在 0~100', () => {
    expect(percent(50, 100)).toBe(50)
    expect(percent(0, 100)).toBe(0)
    expect(percent(150, 100)).toBe(100)
    expect(percent(-10, 100)).toBe(0)
  })

  it('分母为 0 或缺失时返回 0（不产生 Infinity/NaN）', () => {
    expect(percent(10, 0)).toBe(0)
    expect(percent(10, null)).toBe(0)
    expect(percent(null, 100)).toBe(0)
    expect(percent(undefined, undefined)).toBe(0)
  })
})
