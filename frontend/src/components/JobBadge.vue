<script setup lang="ts">
/** 任务状态徽章（状态枚举严格来自 API.md §0.2）。 */
import { computed } from 'vue'
import { jobStatusLabel, jobStatusTone } from '@/api/jobs'

const props = withDefaults(
  defineProps<{
    status?: string | null
    /** 是否显示状态点。 */
    dot?: boolean
  }>(),
  { status: null, dot: true },
)

const tone = computed(() => jobStatusTone(props.status))
const label = computed(() => jobStatusLabel(props.status))
const pulsing = computed(() => props.status === 'RUNNING' || props.status === 'RETRYING')
</script>

<template>
  <span class="badge badge-dot" :class="[`badge-${tone}`, { pulsing }]">
    <slot>{{ label }}</slot>
  </span>
</template>

<style scoped>
.pulsing::before {
  animation: badge-pulse 1.4s ease-in-out infinite;
}
@keyframes badge-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.25;
  }
}
</style>
