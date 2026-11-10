<script setup lang="ts">
/** 404：不存在的路由。 */
import { RouterLink } from 'vue-router'
import EmptyState from '@/components/EmptyState.vue'
</script>

<template>
  <div class="page">
    <EmptyState
      icon="✕"
      title="页面不存在"
      description="你访问的地址没有对应的页面。可以从项目列表或任务中心重新进入。"
    >
      <RouterLink to="/" class="btn btn-primary">返回项目列表</RouterLink>
      <RouterLink to="/jobs" class="btn btn-ghost">任务中心</RouterLink>
    </EmptyState>
  </div>
</template>

<style scoped>
.page {
  padding: var(--space-6) var(--space-6);
  display: grid;
  place-items: center;
  min-height: 60vh;
}
</style>
