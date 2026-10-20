<script setup lang="ts">
/**
 * 应用外壳：左侧固定全局导航 + 主内容区 + 全局提示宿主。
 * 剧集工作台在路由内部再套一层 WorkbenchLayout（顶部剧集信息条 + 阶段导航）。
 */
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useConfigStore } from '@/stores/config'
import { useJobsStore } from '@/stores/jobs'
import ToastHost from '@/components/ToastHost.vue'

const route = useRoute()
const config = useConfigStore()
const jobs = useJobsStore()
const { healthOk, health, quota } = storeToRefs(config)

const NAV = [
  /**
   * PRD §5.1-1 工作区页。
   * 放在最前面作为「首屏落地」，但 `/` 依然保持项目列表不变——
   * 现有 12 条路由的语义与书签都不受影响。
   * 匹配 `/workspace` 与 `/workspace/assets`，因此「资产库」高亮在同一项下。
   */
  { to: '/workspace', label: '工作区', icon: '◎', match: (p: string) => p.startsWith('/workspace') },
  { to: '/', label: '项目', icon: '▤', match: (p: string) => p === '/' || p.startsWith('/projects') },
  { to: '/jobs', label: '任务中心', icon: '≡', match: (p: string) => p.startsWith('/jobs') },
  { to: '/cost', label: '成本与记录', icon: '¥', match: (p: string) => p.startsWith('/cost') },
  { to: '/settings', label: '设置', icon: '⚙', match: (p: string) => p.startsWith('/settings') },
]

const activePath = computed(() => route.path)

const healthLabel = computed(() => {
  if (healthOk.value === null) return '检测中…'
  if (healthOk.value) return '后端在线'
  return '后端离线'
})

const healthTone = computed(() => {
  if (healthOk.value === null) return 'muted'
  return healthOk.value ? 'ok' : 'danger'
})

const version = computed(() => health.value?.version ?? null)

onMounted(async () => {
  await config.load()
  void config.loadHealth()
  void config.loadQuota()
  void jobs.load()
})
</script>

<template>
  <div class="shell">
    <aside class="shell-nav">
      <RouterLink to="/" class="brand">
        <span class="brand-mark" aria-hidden="true">漫</span>
        <span class="brand-text">
          <span class="brand-name">hoyo漫剧</span>
          <span class="brand-sub">AI 漫剧制作台</span>
        </span>
      </RouterLink>

      <nav class="nav-list" aria-label="主导航">
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: item.match(activePath) }"
        >
          <span class="nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="nav-spacer" />

      <div class="nav-foot">
        <div class="health">
          <span class="badge badge-dot" :class="`badge-${healthTone}`">{{ healthLabel }}</span>
          <span v-if="version" class="mono tiny dim-2">v{{ version }}</span>
        </div>
        <div v-if="quota" class="quota-mini mono tiny dim">
          <span>{{ (quota.remaining_cents / 100).toFixed(2) }}</span>
          <span class="dim-2">/ {{ (quota.limit_cents / 100).toFixed(2) }} 元可用</span>
        </div>
        <button
          type="button"
          class="btn btn-ghost btn-sm btn-block"
          :disabled="config.loading"
          @click="config.load(true).then(() => config.loadHealth())"
        >
          {{ config.loading ? '刷新中…' : '刷新配置' }}
        </button>
      </div>
    </aside>

    <main class="shell-main">
      <RouterView v-slot="{ Component }">
        <component :is="Component" />
      </RouterView>
    </main>

    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: var(--nav-w) minmax(0, 1fr);
  height: 100%;
}

.shell-nav {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 12px;
  background: linear-gradient(180deg, rgba(23, 26, 34, 0.9), rgba(11, 13, 18, 0.9));
  border-right: 1px solid var(--line);
  height: 100%;
  overflow: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px 12px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-weight: 800;
  font-size: 16px;
  color: #08111a;
  background: var(--accent-grad);
  flex: 0 0 auto;
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
  min-width: 0;
}
.brand-name {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.brand-sub {
  font-size: 11px;
  color: var(--text-2);
  letter-spacing: 0.04em;
}

.nav-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: 13.5px;
  font-weight: 550;
  border: 1px solid transparent;
  transition: all var(--dur) var(--ease);
}
.nav-item:hover {
  background: var(--neutral-soft);
  color: var(--text-0);
}
.nav-item.active {
  background: var(--accent-soft);
  border-color: var(--line-accent);
  color: var(--text-0);
}
.nav-item.active .nav-icon {
  color: var(--accent);
}
.nav-icon {
  width: 16px;
  text-align: center;
  color: var(--text-2);
  font-size: 13px;
}

.nav-spacer {
  flex: 1 1 auto;
}

.nav-foot {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.health {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.quota-mini {
  display: flex;
  align-items: baseline;
  gap: 5px;
}

.shell-main {
  min-width: 0;
  height: 100%;
  overflow: auto;
}
</style>
