import { http } from './client'
import type {
  EpisodeOverview,
  GateId,
  ResumeInput,
  WorkflowGraphNode,
  WorkflowLogEntry,
  WorkflowRun,
} from '@/types'

/** 工作流与门控（API.md §9）。 */
export const workflowApi = {
  /** 启动工作流并自动跑到第一个门控（后端 body 必填，故显式传 auto_run）。 */
  start: (episodeId: string) =>
    http.post<WorkflowRun>(`/episodes/${episodeId}/workflow/start`, { auto_run: true }),

  /** 当前 run；尚未启动时后端返回 null（契约见 docs/API.md §9）。 */
  get: (episodeId: string) => http.get<WorkflowRun | null>(`/episodes/${episodeId}/workflow`),

  /** 门控决策；版本不一致会返回 CHECKPOINT_STALE。 */
  resume: (episodeId: string, body: ResumeInput) =>
    http.post<WorkflowRun>(`/episodes/${episodeId}/workflow/resume`, body),

  retry: (episodeId: string, node: string) =>
    http.post<WorkflowRun>(`/episodes/${episodeId}/workflow/retry`, { node }),

  rollback: (episodeId: string, toNode: string) =>
    http.post<WorkflowRun>(`/episodes/${episodeId}/workflow/rollback`, { to_node: toNode }),

  logs: (episodeId: string) =>
    http.get<WorkflowLogEntry[] | { items: WorkflowLogEntry[] }>(
      `/episodes/${episodeId}/workflow/logs`,
    ),

  graph: (episodeId: string) =>
    http.get<WorkflowGraphNode[] | { nodes: WorkflowGraphNode[] }>(
      `/episodes/${episodeId}/workflow/graph`,
    ),

  overview: (episodeId: string) =>
    http.get<EpisodeOverview>(`/episodes/${episodeId}/overview`),
}

/** 门控中文名。 */
export const GATE_LABEL: Record<string, string> = {
  gate_script_confirmation: '剧本确认',
  gate_character_confirmation: '角色与画风确认',
  gate_shot_confirmation: '分镜画面确认',
  gate_video_cost_confirmation: '视频成本确认',
  gate_video_confirmation: '视频片段确认',
  gate_export_confirmation: '导出确认',
}

/** 门控 → 对应工作台阶段路由。 */
export const GATE_ROUTE: Record<string, string> = {
  gate_script_confirmation: 'story',
  gate_character_confirmation: 'characters',
  gate_shot_confirmation: 'shots',
  gate_video_cost_confirmation: 'video',
  gate_video_confirmation: 'video',
  gate_export_confirmation: 'render',
}

export function gateLabel(gate?: string | null): string {
  if (!gate) return '无待确认门控'
  return GATE_LABEL[gate] ?? gate
}

export function isGateId(value: unknown): value is GateId {
  return typeof value === 'string' && value.startsWith('gate_')
}
