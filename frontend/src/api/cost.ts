import { http } from './client'
import { asList } from './projects'
import type { CostLedgerEntry, CostSummary, Paginated, Quota } from '@/types'

/** 成本流水分页查询参数（后端支持 episode_id / project_id / kind / page / page_size）。 */
export interface CostLedgerQuery {
  episode_id?: string
  project_id?: string
  kind?: string
  page?: number
  page_size?: number
}

/** 成本与配额（API.md §13）。 */
export const costApi = {
  ledger: (query: CostLedgerQuery = {}) =>
    http.get<Paginated<CostLedgerEntry> | CostLedgerEntry[] | { items: CostLedgerEntry[] }>(
      '/cost/ledger',
      {
        episode_id: query.episode_id,
        project_id: query.project_id,
        kind: query.kind,
        page: query.page,
        page_size: query.page_size,
      },
    ),

  summary: (query: { episode_id?: string; project_id?: string } = {}) =>
    http.get<CostSummary>('/cost/summary', query),

  quota: () => http.get<Quota>('/quota'),
}

export async function fetchLedger(query: CostLedgerQuery = {}): Promise<CostLedgerEntry[]> {
  return asList<CostLedgerEntry>(await costApi.ledger(query))
}

/** 成本流水分页（后端返回 `{items,total,page,page_size}`，裸数组时本地兜底）。 */
export async function fetchLedgerPage(query: CostLedgerQuery = {}): Promise<Paginated<CostLedgerEntry>> {
  const raw = await costApi.ledger(query)
  const page = query.page ?? 1
  const pageSize = query.page_size ?? 50
  if (Array.isArray(raw)) {
    return { items: raw, total: raw.length, page, page_size: pageSize }
  }
  const paginated = raw as Partial<Paginated<CostLedgerEntry>>
  const items = Array.isArray(paginated.items) ? paginated.items : []
  return {
    items,
    total: paginated.total ?? items.length,
    page: paginated.page ?? page,
    page_size: paginated.page_size ?? pageSize,
  }
}

/** 配额快照（`GET /quota`），额度/已用/预留全部来自后端，前端不写死。 */
export async function fetchQuota(): Promise<Quota> {
  return costApi.quota()
}

/** 成本汇总（`GET /cost/summary`）。 */
export async function fetchCostSummary(
  query: { episode_id?: string; project_id?: string } = {},
): Promise<CostSummary> {
  return costApi.summary(query)
}

/** 阶段中文名（与 Episode.stage 对齐）。 */
export const STAGE_COST_LABEL: Record<string, string> = {
  story: '故事与剧本',
  script: '剧本',
  character: '角色',
  style: '画风',
  image: '分镜图',
  video: '视频',
  tts: '配音',
  audio: '音频',
  render: '合成',
  export: '导出',
  package: '素材包',
}

export function stageCostLabel(stage?: string | null): string {
  if (!stage) return '未分类'
  return STAGE_COST_LABEL[stage] ?? stage
}

/** 分 → 人民币显示。金额一律以最小货币单位整数（分）传输（API.md 开头约定）。 */
export function formatCents(cents?: number | null): string {
  if (cents === null || cents === undefined) return '—'
  const yuan = cents / 100
  return `¥${yuan.toFixed(2)}`
}

/** 分 → 带符号的人民币显示（流水金额正负一眼可辨）。 */
export function formatCentsSigned(cents?: number | null): string {
  if (cents === null || cents === undefined) return '—'
  const sign = cents > 0 ? '+' : cents < 0 ? '−' : ''
  return `${sign}${formatCents(Math.abs(cents))}`
}

/**
 * 分 → 供应商积分。
 *
 * ToAPIs 的 1 积分 = $0.005，按汇率折算约 3.6 分。界面上以积分为主，
 * 这样「生成一条 4 秒视频消耗 33 积分」和供应商后台是同一个数，
 * 不用心算「¥1.19 到底等于多少积分」。
 */
export function formatCredits(credits?: number | null): string {
  if (credits === null || credits === undefined) return '—'
  const value = Math.abs(credits) >= 100 ? credits.toFixed(0) : credits.toFixed(2)
  return `${value} 积分`
}

/** 分 → 积分（四舍五入到 2 位，用于把旧接口的金额快速换成积分口径）。 */
export function centsToCredits(cents?: number | null, centsPerCredit = 3.6): number {
  if (cents === null || cents === undefined) return 0
  return Math.round((cents / centsPerCredit) * 100) / 100
}

/** 流水类型（API.md §13：RESERVE 预留 / SETTLE 结算 / RELEASE 释放）。 */
export const LEDGER_KIND_LABEL: Record<string, string> = {
  RESERVE: '预留',
  SETTLE: '结算',
  RELEASE: '释放',
}

export const LEDGER_KIND_OPTIONS = ['RESERVE', 'SETTLE', 'RELEASE'] as const

export function ledgerKindLabel(kind?: string | null): string {
  if (!kind) return '未知'
  return LEDGER_KIND_LABEL[kind] ?? kind
}

/** 流水类型 → 徽章色调。 */
export function ledgerKindTone(kind?: string | null): 'warn' | 'danger' | 'ok' | 'info' {
  switch (kind) {
    case 'RESERVE':
      return 'warn'
    case 'SETTLE':
      return 'danger'
    case 'RELEASE':
      return 'ok'
    default:
      return 'info'
  }
}
