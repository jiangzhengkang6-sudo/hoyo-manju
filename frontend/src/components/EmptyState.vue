<script setup lang="ts">
/** 通用空态：图标 + 标题 + 说明 + 可选操作插槽。 */
withDefaults(
  defineProps<{
    icon?: string
    title?: string
    description?: string
    compact?: boolean
  }>(),
  {
    icon: '◇',
    title: '暂无数据',
    description: '',
    compact: false,
  },
)
</script>

<template>
  <div class="empty" :class="{ compact }">
    <div class="empty-icon" aria-hidden="true">{{ icon }}</div>
    <div class="empty-title">{{ title }}</div>
    <p v-if="description" class="empty-desc">{{ description }}</p>
    <div v-if="$slots.default" class="empty-actions">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.empty-icon {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  border: 1px solid var(--line);
  background: var(--panel-2);
  font-size: 18px;
  color: var(--text-2);
  background-image: var(--accent-grad);
  -webkit-background-clip: text;
  background-clip: text;
}
.empty-desc {
  max-width: 420px;
  font-size: 12.5px;
  line-height: 1.6;
}
.empty-actions {
  margin-top: 4px;
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: center;
}
.empty.compact {
  padding: 24px 16px;
  gap: 7px;
}
.empty.compact .empty-icon {
  width: 32px;
  height: 32px;
  font-size: 14px;
}
</style>
