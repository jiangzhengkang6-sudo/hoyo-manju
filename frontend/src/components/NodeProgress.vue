<script setup lang="ts">
/**
 * 工作流节点进度条：展示后端 `/workflow/graph` 返回的节点清单与状态，
 * 并高亮当前节点与待确认门控。
 * `vertical` 为左侧栏的紧凑纵向变体（默认横向，保持既有调用方行为不变）。
 */
import { computed } from 'vue'
import { gateLabel } from '@/api/workflow'
import EmptyState from './EmptyState.vue'
import type { WorkflowGraphNode } from '@/types'

const props = withDefaults(
  defineProps<{
    nodes: WorkflowGraphNode[]
    current?: string | null
    pendingGate?: string | null
    busy?: boolean
    /** 是否可点击节点（回退到该节点）。 */
    clickable?: boolean
    /** 纵向紧凑变体：用于工作台左侧栏。 */
    vertical?: boolean
  }>(),
  { current: null, pendingGate: null, busy: false, clickable: false, vertical: false },
)

const emit = defineEmits<{ select: [node: string] }>()

interface NodeView {
  key: string
  label: string
  status: string
  tone: string
  isCurrent: boolean
  hasGate: boolean
  done: boolean
}

const TONE: Record<string, string> = {
  SUCCEEDED: 'ok',
  COMPLETED: 'ok',
  DONE: 'ok',
  RUNNING: 'info',
  STARTED: 'info',
  WAITING_CONFIRMATION: 'warn',
  WAITING: 'warn',
  PENDING: 'muted',
  QUEUED: 'muted',
  FAILED: 'danger',
  ERROR: 'danger',
  SKIPPED: 'muted',
  BLOCKED: 'danger',
}

const STATUS_LABEL: Record<string, string> = {
  SUCCEEDED: '完成',
  COMPLETED: '完成',
  DONE: '完成',
  RUNNING: '执行中',
  STARTED: '执行中',
  WAITING_CONFIRMATION: '等待确认',
  WAITING: '等待确认',
  PENDING: '待执行',
  QUEUED: '排队',
  FAILED: '失败',
  ERROR: '失败',
  SKIPPED: '跳过',
  BLOCKED: '阻断',
}

const views = computed<NodeView[]>(() =>
  props.nodes.map((n, i) => {
    const key = String(n.node ?? n.id ?? `node_${i}`)
    const status = String(n.status ?? 'PENDING').toUpperCase()
    const tone = TONE[status] ?? 'muted'
    return {
      key,
      label: String(n.label ?? key),
      status,
      tone,
      isCurrent: props.current === key,
      hasGate: Boolean(n.gate),
      done: tone === 'ok',
    }
  }),
)

const doneCount = computed(() => views.value.filter((v) => v.done).length)

function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status
}
</script>

<template>
  <div class="node-progress" :class="{ vertical }">
    <div class="node-head">
      <div class="row">
        <span class="section-title">工作流节点</span>
        <span class="mono tiny dim">{{ doneCount }}/{{ views.length }}</span>
      </div>
      <span v-if="pendingGate" class="badge badge-warn badge-dot">
        待确认 · {{ gateLabel(pendingGate) }}
      </span>
      <span v-else-if="current" class="badge badge-info badge-dot">
        当前 · {{ current }}
      </span>
      <span v-else class="badge">未启动</span>
    </div>

    <EmptyState
      v-if="!views.length"
      compact
      icon="⑃"
      title="暂无节点信息"
      description="工作流尚未启动，或 /workflow/graph 未返回节点清单。"
    >
      <slot name="empty-action" />
    </EmptyState>

    <ol v-else class="node-track" :class="{ vertical }">
      <li
        v-for="(node, i) in views"
        :key="node.key"
        class="node-item"
        :class="[`tone-${node.tone}`, { current: node.isCurrent, clickable }]"
        :title="`${node.label} · ${statusLabel(node.status)}`"
        @click="clickable && !busy && emit('select', node.key)"
      >
        <span v-if="i > 0" class="node-link" :class="{ filled: node.done || node.isCurrent }" />
        <span class="node-dot">
          <span v-if="node.done" class="node-check">✓</span>
          <span v-else-if="node.tone === 'danger'" class="node-check">×</span>
        </span>
        <span class="node-label ellipsis">{{ node.label }}</span>
        <span v-if="node.hasGate" class="node-gate" aria-label="门控节点">◆</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.node-progress {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.node-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.node-track {
  list-style: none;
  margin: 0;
  padding: 4px 2px 10px;
  display: flex;
  align-items: flex-start;
  gap: 0;
  overflow-x: auto;
  scrollbar-width: thin;
}
.node-item {
  position: relative;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  min-width: 78px;
  padding: 0 6px;
  cursor: default;
}
.node-item.clickable {
  cursor: pointer;
}
.node-item.clickable:hover .node-dot {
  border-color: var(--line-accent);
  background: var(--accent-soft);
}
.node-link {
  position: absolute;
  top: 10px;
  right: 50%;
  left: -50%;
  height: 1px;
  background: var(--line-strong);
  z-index: 0;
}
.node-link.filled {
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.5), rgba(167, 139, 250, 0.5));
}
.node-dot {
  position: relative;
  z-index: 1;
  width: 21px;
  height: 21px;
  border-radius: 50%;
  border: 1px solid var(--line-strong);
  background: var(--panel-2);
  display: grid;
  place-items: center;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-2);
  transition: all var(--dur) var(--ease);
}
.node-label {
  font-size: 11px;
  color: var(--text-2);
  max-width: 84px;
  text-align: center;
}
.node-gate {
  position: absolute;
  top: -3px;
  right: calc(50% - 17px);
  font-size: 9px;
  color: var(--warn);
}
.tone-ok .node-dot {
  background: var(--ok-soft);
  border-color: rgba(52, 211, 153, 0.45);
  color: var(--ok);
}
.tone-ok .node-label {
  color: var(--text-1);
}
.tone-info .node-dot {
  background: var(--accent-soft);
  border-color: var(--line-accent);
  color: var(--accent);
  box-shadow: 0 0 0 4px rgba(56, 189, 248, 0.08);
}
.tone-warn .node-dot {
  background: var(--warn-soft);
  border-color: rgba(251, 191, 36, 0.5);
  color: var(--warn);
  box-shadow: 0 0 0 4px rgba(251, 191, 36, 0.08);
}
.tone-danger .node-dot {
  background: var(--danger-soft);
  border-color: rgba(248, 113, 113, 0.5);
  color: var(--danger);
}
.node-item.current .node-label {
  color: var(--text-0);
  font-weight: 600;
}
.node-item.current .node-dot {
  transform: scale(1.12);
}

/* ------------------------------------------------------------------ *
 * 纵向紧凑变体（工作台左侧栏）
 * ------------------------------------------------------------------ */

.node-progress.vertical {
  gap: 6px;
}
.node-track.vertical {
  flex-direction: column;
  align-items: stretch;
  gap: 2px;
  overflow: visible;
  padding: 2px 0 0;
}
.node-track.vertical .node-item {
  flex-direction: row;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  min-width: 0;
  padding: 4px 6px;
  border-radius: var(--radius-xs);
  border: 1px solid transparent;
  transition: all var(--dur) var(--ease);
}
.node-track.vertical .node-item:hover {
  background: var(--neutral-soft);
}
.node-track.vertical .node-item.current {
  border-color: var(--line-accent);
  background: var(--accent-soft);
}
.node-track.vertical .node-link {
  display: none;
}
.node-track.vertical .node-dot {
  width: 18px;
  height: 18px;
  font-size: 10px;
  flex: 0 0 auto;
}
.node-track.vertical .node-label {
  max-width: none;
  text-align: left;
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12px;
}
.node-track.vertical .node-item.current .node-dot {
  transform: none;
}
.node-track.vertical .node-gate {
  position: static;
  flex: 0 0 auto;
}
</style>
