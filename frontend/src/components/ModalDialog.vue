<script setup lang="ts">
/** 通用弹窗：Teleport 到 body，支持 Esc 关闭与点击遮罩关闭。 */
import { onBeforeUnmount, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title?: string
    /** 最大宽度（px）。 */
    width?: number
    /** 是否允许点击遮罩关闭。 */
    maskClosable?: boolean
  }>(),
  { title: '', width: 560, maskClosable: true },
)

const emit = defineEmits<{ close: [] }>()

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
  { immediate: true },
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="overlay"
      @click.self="maskClosable && emit('close')"
    >
      <div class="modal" :style="{ maxWidth: `${width}px` }" role="dialog" aria-modal="true">
        <header class="modal-head">
          <h3 class="title-md">{{ title }}</h3>
          <button type="button" class="modal-close" aria-label="关闭" @click="emit('close')">×</button>
        </header>
        <div class="modal-body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="modal-foot">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  background: var(--panel);
  z-index: 1;
}
.modal-close {
  background: transparent;
  border: 0;
  color: var(--text-2);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.modal-close:hover {
  color: var(--text-0);
}
.modal-body {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.modal-foot {
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  position: sticky;
  bottom: 0;
  background: var(--panel);
}
</style>
