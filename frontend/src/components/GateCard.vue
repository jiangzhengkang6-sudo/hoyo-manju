<script setup lang="ts">
/**
 * 门控确认卡：每个 gate 都有「确认 / 驳回 / 重新生成」三个动作
 * （API.md §9 `POST /workflow/resume` 的 decision = approve | reject | edit）。
 */
import { computed, ref, watch } from 'vue'
import { gateLabel } from '@/api/workflow'
import type { WorkflowRun } from '@/types'

const props = withDefaults(
  defineProps<{
    gate?: string | null
    workflow?: WorkflowRun | null
    /** 是否正在提交。 */
    busy?: boolean
    /** 通过按钮文案。 */
    approveText?: string
    /** 卡片副标题（例如「确认剧本后进入角色与画风」）。 */
    description?: string
    /** 是否允许重新生成（edit）。 */
    allowEdit?: boolean
    /** 是否允许驳回。 */
    allowReject?: boolean
    /** 驳回是否需要填写原因。 */
    requireRejectReason?: boolean
    /** 版本不一致等提示。 */
    notice?: string
  }>(),
  {
    gate: null,
    workflow: null,
    busy: false,
    approveText: '确认通过',
    description: '',
    allowEdit: true,
    allowReject: true,
    requireRejectReason: true,
    notice: '',
  },
)

const emit = defineEmits<{
  approve: []
  reject: [reason: string]
  edit: [instruction: string]
  refresh: []
}>()

type Mode = 'idle' | 'reject' | 'edit'
const mode = ref<Mode>('idle')
const reason = ref('')
const instruction = ref('')

watch(
  () => props.gate,
  () => {
    mode.value = 'idle'
    reason.value = ''
    instruction.value = ''
  },
)

const isWaiting = computed(() => Boolean(props.gate))
const title = computed(() => gateLabel(props.gate))
const checkpointId = computed(() => props.workflow?.checkpoint_id ?? null)
const targetVersion = computed(() => {
  const v = props.workflow?.state?.version
  return typeof v === 'number' ? v : null
})

const rejectDisabled = computed(
  () => props.busy || (props.requireRejectReason && reason.value.trim().length === 0),
)
const editDisabled = computed(() => props.busy || instruction.value.trim().length === 0)

function submitReject(): void {
  if (rejectDisabled.value) return
  emit('reject', reason.value.trim())
  mode.value = 'idle'
  reason.value = ''
}

function submitEdit(): void {
  if (editDisabled.value) return
  emit('edit', instruction.value.trim())
  mode.value = 'idle'
  instruction.value = ''
}
</script>

<template>
  <section class="gate-card" :class="{ waiting: isWaiting }">
    <header class="gate-head">
      <div class="gate-title-wrap">
        <span class="gate-mark" aria-hidden="true">◆</span>
        <div>
          <div class="gate-title">
            {{ isWaiting ? title : '当前无待确认门控' }}
            <span v-if="isWaiting" class="badge badge-warn" style="margin-left: 8px">等待确认</span>
          </div>
          <div class="gate-sub tiny dim">
            <template v-if="isWaiting">
              <span v-if="description">{{ description }}</span>
              <span v-else>确认后工作流将继续推进到下一阶段。</span>
            </template>
            <template v-else>工作流会在需要你决策时停在此处。</template>
          </div>
        </div>
      </div>
      <button
        type="button"
        class="btn btn-sm btn-ghost"
        :disabled="busy"
        title="重新拉取工作流状态"
        @click="emit('refresh')"
      >
        刷新状态
      </button>
    </header>

    <div v-if="isWaiting" class="gate-meta tiny mono">
      <span v-if="workflow?.id">run {{ workflow.id }}</span>
      <span v-if="checkpointId">checkpoint {{ checkpointId }}</span>
      <span v-if="targetVersion !== null">version {{ targetVersion }}</span>
      <span v-if="workflow?.current_node">node {{ workflow.current_node }}</span>
    </div>

    <p v-if="notice" class="gate-notice tiny">{{ notice }}</p>

    <div class="gate-body">
      <slot />
    </div>

    <footer class="gate-actions">
      <template v-if="mode === 'idle'">
        <button
          type="button"
          class="btn btn-primary"
          :disabled="busy || !isWaiting"
          @click="emit('approve')"
        >
          {{ busy ? '提交中…' : approveText }}
        </button>
        <button
          v-if="allowReject"
          type="button"
          class="btn btn-danger"
          :disabled="busy || !isWaiting"
          @click="mode = 'reject'"
        >
          驳回
        </button>
        <button
          v-if="allowEdit"
          type="button"
          class="btn"
          :disabled="busy || !isWaiting"
          @click="mode = 'edit'"
        >
          重新生成
        </button>
        <div class="grow" />
        <slot name="actions" />
      </template>

      <template v-else-if="mode === 'reject'">
        <textarea
          v-model="reason"
          class="textarea"
          rows="2"
          placeholder="请说明驳回原因（会作为决策 payload 回传后端）"
        />
        <div class="row">
          <button
            type="button"
            class="btn btn-danger btn-sm"
            :disabled="rejectDisabled"
            @click="submitReject"
          >
            提交驳回
          </button>
          <button type="button" class="btn btn-ghost btn-sm" @click="mode = 'idle'">取消</button>
        </div>
      </template>

      <template v-else>
        <textarea
          v-model="instruction"
          class="textarea"
          rows="2"
          placeholder="请填写重做指令（例如：节奏再快一点，第 3 镜改成全景）"
        />
        <div class="row">
          <button
            type="button"
            class="btn btn-sm"
            :disabled="editDisabled"
            @click="submitEdit"
          >
            提交重做指令
          </button>
          <button type="button" class="btn btn-ghost btn-sm" @click="mode = 'idle'">取消</button>
        </div>
      </template>
    </footer>
  </section>
</template>

<style scoped>
.gate-card {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  overflow: hidden;
}
.gate-card.waiting {
  border-color: rgba(251, 191, 36, 0.32);
  background:
    linear-gradient(180deg, rgba(251, 191, 36, 0.06), transparent 46%),
    var(--panel);
}
.gate-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
}
.gate-title-wrap {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.gate-mark {
  color: var(--warn);
  font-size: 13px;
  margin-top: 3px;
}
.gate-title {
  font-size: 14.5px;
  font-weight: 640;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.gate-sub {
  margin-top: 2px;
}
.gate-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  padding: 0 16px 10px;
  color: var(--text-3);
}
.gate-notice {
  margin: 0 16px 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid rgba(251, 191, 36, 0.32);
  background: var(--warn-soft);
  color: var(--warn);
}
.gate-body {
  padding: 0 16px;
}
.gate-body:empty {
  display: none;
}
.gate-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 14px 16px;
  border-top: 1px solid var(--line);
  margin-top: 14px;
}
.gate-actions .textarea {
  width: 100%;
}
</style>
