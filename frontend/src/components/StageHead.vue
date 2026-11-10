<script setup lang="ts">
/**
 * 剧集阶段页头。
 *
 * 六个工作台页面此前都直接以 GateCard 开头，没有页面标题——用户只能靠左上角
 * 的高亮去猜「我现在在哪一阶段」。这里统一补上「阶段序号 + 标题 + 一句说明」，
 * 并给右侧留一个操作槽，让每个页面的第一屏都能回答：我在哪、这一步要做什么。
 */
defineProps<{
  /** 两位阶段序号，如 '03' */
  index: string
  title: string
  description?: string
}>()
</script>

<template>
  <header class="stage-head">
    <div class="stage-head-main">
      <span class="stage-index">阶段 {{ index }} / 06</span>
      <h2 class="stage-title">{{ title }}</h2>
      <p v-if="description">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="stage-head-actions">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.stage-head-main {
  min-width: 0;
}
.stage-title {
  font-size: var(--fs-24);
  font-weight: 650;
  line-height: 1.2;
  margin-top: 2px;
}
.stage-head-actions {
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
  flex-wrap: wrap;
}
</style>
