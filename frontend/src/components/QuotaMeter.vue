<script setup lang="ts">
/** 配额仪表：额度 / 已用 / 冻结 / 剩余 + 并发占用。数值全部来自 `GET /quota`。 */
import { computed } from 'vue'
import { formatCents } from '@/api/cost'
import { percent } from '@/utils/format'
import SkeletonBlock from './SkeletonBlock.vue'
import type { Quota } from '@/types'

const props = withDefaults(
  defineProps<{
    quota: Quota | null
    loading?: boolean
    compact?: boolean
  }>(),
  { loading: false, compact: false },
)

const usedPercent = computed(() => {
  const q = props.quota
  if (!q || !q.limit_cents) return 0
  return percent((q.used_cents ?? 0) + (q.reserved_cents ?? 0), q.limit_cents)
})

const barTone = computed(() => {
  if (usedPercent.value >= 95) return 'danger'
  if (usedPercent.value >= 75) return 'warn'
  return ''
})

const concurrentPercent = computed(() => {
  const q = props.quota
  if (!q || !q.concurrent_limit) return 0
  return percent(q.running_jobs, q.concurrent_limit)
})
</script>

<template>
  <div class="quota-meter" :class="{ compact }">
    <SkeletonBlock v-if="loading && !quota" :rows="2" :height="14" />
    <template v-else-if="quota">
      <div class="row-between">
        <span class="section-title">成本配额</span>
        <span class="mono tiny" :class="barTone === 'danger' ? 'qm-danger' : 'dim'">
          {{ formatCents(quota.remaining_cents) }} 可用
        </span>
      </div>

      <div class="progress progress-lg">
        <div class="progress-bar" :class="barTone" :style="{ width: `${usedPercent}%` }" />
      </div>

      <div class="qm-grid mono tiny">
        <span>额度 {{ formatCents(quota.limit_cents) }}</span>
        <span>已用 {{ formatCents(quota.used_cents) }}</span>
        <span v-if="quota.reserved_cents">冻结 {{ formatCents(quota.reserved_cents) }}</span>
        <span>占用 {{ usedPercent.toFixed(1) }}%</span>
      </div>

      <div v-if="!compact" class="qm-concurrent">
        <div class="row-between tiny dim">
          <span>并发任务</span>
          <span class="mono">{{ quota.running_jobs }} / {{ quota.concurrent_limit }}</span>
        </div>
        <div class="progress">
          <div
            class="progress-bar"
            :class="{ warn: concurrentPercent >= 80 }"
            :style="{ width: `${concurrentPercent}%` }"
          />
        </div>
      </div>
    </template>
    <div v-else class="tiny dim">配额信息不可用（后端未返回 /quota）。</div>
  </div>
</template>

<style scoped>
.quota-meter {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.quota-meter.compact {
  gap: 6px;
}
.qm-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  color: var(--text-2);
}
.qm-danger {
  color: var(--danger);
}
.qm-concurrent {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 2px;
}
</style>
