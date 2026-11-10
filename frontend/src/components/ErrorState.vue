<script setup lang="ts">
/** 统一错误态：展示 message / code / 字段级 issues / trace_id，retryable 时给重试按钮。 */
import { computed } from 'vue'
import { ApiError } from '@/api/errors'

const props = withDefaults(
  defineProps<{
    error: ApiError | null | undefined
    /** 业务上下文，例如「加载分镜表」。 */
    context?: string
    compact?: boolean
  }>(),
  { context: '', compact: false },
)

const emit = defineEmits<{ retry: [] }>()

const title = computed(() => props.error?.title ?? '出错了')
const message = computed(() => props.error?.message ?? '未知错误')
const issues = computed(() => props.error?.issues ?? [])
const traceId = computed(() => props.error?.traceId ?? null)
const retryable = computed(() => Boolean(props.error?.retryable))
const isNetwork = computed(() => props.error?.code === 'NETWORK_ERROR')
</script>

<template>
  <div class="error-state" :class="{ compact }" role="alert">
    <div class="error-head">
      <span class="error-mark" aria-hidden="true">!</span>
      <div>
        <div class="error-title">
          <span v-if="context">{{ context }} · </span>{{ title }}
        </div>
        <div class="error-code mono">{{ error?.code ?? 'UNKNOWN' }}</div>
      </div>
    </div>

    <p class="error-message">{{ message }}</p>

    <p v-if="isNetwork" class="error-hint">
      请确认后端服务已启动（默认 <span class="mono">http://127.0.0.1:18437</span>），或检查
      <span class="mono">VITE_API_BASE</span> 配置。
    </p>

    <ul v-if="issues.length" class="error-issues">
      <li v-for="(issue, i) in issues" :key="i">
        <span class="issue-label">{{ issue.label || issue.path || '字段' }}</span>
        <span class="issue-msg">{{ issue.message }}</span>
      </li>
    </ul>

    <div class="error-foot">
      <button v-if="retryable" type="button" class="btn btn-sm btn-primary" @click="emit('retry')">
        重试
      </button>
      <button v-else type="button" class="btn btn-sm btn-ghost" @click="emit('retry')">
        重新加载
      </button>
      <span v-if="traceId" class="error-trace mono" :title="traceId">
        trace_id {{ traceId }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.error-state {
  border: 1px solid rgba(224, 122, 106, 0.32);
  background: linear-gradient(180deg, rgba(224, 122, 106, 0.07), rgba(224, 122, 106, 0.02));
  border-radius: var(--radius);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.error-state.compact {
  padding: var(--space-3);
  gap: var(--space-2);
}
.error-head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}
.error-mark {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: var(--fs-12);
  font-weight: 700;
  background: var(--danger-soft);
  color: var(--danger);
  border: 1px solid rgba(224, 122, 106, 0.4);
  flex: 0 0 auto;
  margin-top: 2px;
}
.error-title {
  font-size: var(--fs-13);
  font-weight: 620;
  color: #f3bdb2;
}
.error-code {
  font-size: var(--fs-11);
  color: var(--text-2);
  letter-spacing: 0.04em;
}
.error-message {
  font-size: var(--fs-12);
  color: var(--text-1);
  word-break: break-word;
}
.error-hint {
  font-size: var(--fs-12);
  color: var(--text-2);
  background: rgba(243, 237, 227, 0.03);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
}
.error-issues {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  font-size: var(--fs-12);
  max-height: 180px;
  overflow: auto;
}
.error-issues li {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-2);
  border-radius: var(--radius-xs);
  background: rgba(243, 237, 227, 0.03);
}
.issue-label {
  color: var(--warn);
  flex: 0 0 auto;
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.issue-msg {
  color: var(--text-1);
}
.error-foot {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.error-trace {
  font-size: var(--fs-11);
  color: var(--text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
</style>
