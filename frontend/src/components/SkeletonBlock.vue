<script setup lang="ts">
/** 骨架屏：加载态统一占位，避免白屏。 */
withDefaults(
  defineProps<{
    /** 行数。 */
    rows?: number
    /** 高度（px）。 */
    height?: number
    /** 是否渲染为卡片网格。 */
    variant?: 'lines' | 'cards' | 'portrait'
  }>(),
  { rows: 3, height: 14, variant: 'lines' },
)
</script>

<template>
  <div v-if="variant === 'lines'" class="skeleton-lines" aria-busy="true" aria-label="加载中">
    <div
      v-for="i in rows"
      :key="i"
      class="skeleton"
      :style="{ height: `${height}px`, width: i === rows ? '62%' : '100%' }"
    />
  </div>

  <div v-else-if="variant === 'cards'" class="skeleton-cards" aria-busy="true" aria-label="加载中">
    <div v-for="i in rows" :key="i" class="skeleton skeleton-card" />
  </div>

  <div v-else class="skeleton-portraits" aria-busy="true" aria-label="加载中">
    <div v-for="i in rows" :key="i" class="skeleton skeleton-portrait" />
  </div>
</template>

<style scoped>
.skeleton-lines {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.skeleton-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(268px, 1fr));
  gap: var(--gap);
}
.skeleton-card {
  height: 132px;
  border-radius: var(--radius);
}
.skeleton-portraits {
  display: flex;
  gap: var(--gap);
  flex-wrap: wrap;
}
.skeleton-portrait {
  width: var(--portrait-w);
  height: var(--portrait-h);
  border-radius: var(--radius);
}
</style>
