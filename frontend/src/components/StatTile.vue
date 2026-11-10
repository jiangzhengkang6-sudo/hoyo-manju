<script setup lang="ts">
/** 数据小卡：数字用等宽字体，标签用弱化色。 */
withDefaults(
  defineProps<{
    label: string
    value: string | number
    hint?: string
    tone?: 'default' | 'ok' | 'warn' | 'danger' | 'accent'
    mono?: boolean
  }>(),
  { hint: '', tone: 'default', mono: true },
)
</script>

<template>
  <div class="stat-tile" :class="`tone-${tone}`">
    <div class="stat-label">{{ label }}</div>
    <div class="stat-value" :class="{ mono }">{{ value }}</div>
    <div v-if="hint" class="stat-hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.stat-tile {
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel);
  min-width: 0;
  transition: border-color var(--dur) var(--ease);
}
.stat-tile:hover {
  border-color: var(--line-strong);
}
.stat-label {
  font-size: var(--fs-11);
  letter-spacing: 0.05em;
  color: var(--text-2);
  text-transform: uppercase;
}
.stat-value {
  font-family: var(--font-display);
  font-variant-numeric: tabular-nums;
  font-size: var(--fs-24);
  font-weight: 600;
  margin-top: 2px;
  letter-spacing: 0;
  line-height: 1.2;
}
.stat-hint {
  font-size: var(--fs-11);
  color: var(--text-2);
  margin-top: 2px;
}
.tone-ok .stat-value {
  color: var(--ok);
}
.tone-warn .stat-value {
  color: var(--warn);
}
.tone-danger .stat-value {
  color: var(--danger);
}
.tone-accent .stat-value {
  background: var(--accent-grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
</style>
