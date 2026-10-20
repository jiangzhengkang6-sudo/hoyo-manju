<script setup lang="ts">
/** 全局提示宿主：接收 api 层 `onToast` 推送与页面主动提示。 */
import { storeToRefs } from 'pinia'
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()
const { items } = storeToRefs(toast)

const ICON: Record<string, string> = {
  success: '✓',
  info: 'i',
  warning: '!',
  error: '×',
}

function copyTrace(traceId: string): void {
  void navigator.clipboard?.writeText(traceId)
  toast.success('已复制 trace_id', traceId)
}
</script>

<template>
  <div class="toast-host" role="region" aria-label="通知">
    <TransitionGroup name="toast">
      <div v-for="t in items" :key="t.id" class="toast" :class="`toast-${t.kind}`">
        <span class="toast-icon" aria-hidden="true">{{ ICON[t.kind] ?? 'i' }}</span>
        <div class="toast-body">
          <div class="toast-title">{{ t.title }}</div>
          <div v-if="t.message" class="toast-message">{{ t.message }}</div>
          <div v-if="t.detail" class="toast-detail">{{ t.detail }}</div>
          <button
            v-if="t.traceId"
            type="button"
            class="toast-trace mono"
            :title="`点击复制 trace_id：${t.traceId}`"
            @click="copyTrace(t.traceId)"
          >
            trace_id {{ t.traceId }}
          </button>
        </div>
        <button
          type="button"
          class="toast-close"
          aria-label="关闭提示"
          @click="toast.dismiss(t.id)"
        >
          ×
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  right: 20px;
  bottom: 20px;
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: 380px;
  max-width: calc(100vw - 40px);
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  display: flex;
  gap: 10px;
  padding: 12px 12px 12px 13px;
  border-radius: var(--radius);
  background: var(--panel);
  border: 1px solid var(--line-strong);
  box-shadow: var(--shadow-2);
  position: relative;
  overflow: hidden;
}

.toast::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent-grad);
}
.toast-success::before {
  background: var(--ok);
}
.toast-warning::before {
  background: var(--warn);
}
.toast-error::before {
  background: var(--danger);
}

.toast-icon {
  flex: 0 0 auto;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 700;
  background: var(--neutral-soft);
  color: var(--text-1);
  margin-top: 1px;
}
.toast-success .toast-icon {
  background: var(--ok-soft);
  color: var(--ok);
}
.toast-warning .toast-icon {
  background: var(--warn-soft);
  color: var(--warn);
}
.toast-error .toast-icon {
  background: var(--danger-soft);
  color: var(--danger);
}

.toast-body {
  flex: 1 1 auto;
  min-width: 0;
}
.toast-title {
  font-size: 13px;
  font-weight: 620;
}
.toast-message {
  font-size: 12.5px;
  color: var(--text-1);
  margin-top: 2px;
  word-break: break-word;
}
.toast-detail {
  font-size: 11.5px;
  color: var(--text-2);
  margin-top: 4px;
  word-break: break-word;
}
.toast-trace {
  margin-top: 6px;
  background: transparent;
  border: 1px dashed var(--line-strong);
  color: var(--text-2);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: var(--radius-xs);
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.toast-trace:hover {
  color: var(--accent);
  border-color: var(--line-accent);
}

.toast-close {
  flex: 0 0 auto;
  background: transparent;
  border: 0;
  color: var(--text-2);
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
  height: 20px;
}
.toast-close:hover {
  color: var(--text-0);
}

.toast-enter-active,
.toast-leave-active {
  transition: all var(--dur-slow) var(--ease);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateX(18px);
}
.toast-leave-active {
  position: absolute;
  right: 0;
  width: 100%;
}
</style>
