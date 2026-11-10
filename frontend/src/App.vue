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
        <span class="nav-label">导航</span>
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: item.match(activePath) }"
        >
          <span class="nav-icon" aria-hidden="true">{{ item.icon }}</span>
          <span class="nav-text">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="nav-spacer" />

      <div class="nav-foot">
        <div class="health">
          <span class="badge badge-dot" :class="`badge-${healthTone}`">{{ healthLabel }}</span>
          <span v-if="version" class="mono tiny dim-2">v{{ version }}</span>
        </div>
        <div v-if="quota" class="quota-mini">
          <span class="quota-label">可用额度</span>
          <span class="quota-value num">{{ (quota.remaining_cents / 100).toFixed(2) }}</span>
          <span class="quota-cap mono tiny dim-2">/ {{ (quota.limit_cents / 100).toFixed(2) }}</span>
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

/* 侧栏：暖墨底 + 右侧发丝线，靠明度与内容区分，不用重阴影 */
.shell-nav {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-5) var(--space-3) 14px;
  background: linear-gradient(180deg, rgba(29, 26, 23, 0.92), rgba(11, 10, 9, 0.94));
  border-right: 1px solid var(--line);
  height: 100%;
  overflow: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 2px 6px 16px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: var(--fs-16);
  color: var(--accent-ink);
  background: var(--accent-grad);
  flex: 0 0 auto;
  box-shadow: inset 0 1px 0 rgba(243, 237, 227, 0.28);
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  min-width: 0;
}
.brand-name {
  font-family: var(--font-display);
  font-size: var(--fs-16);
  font-weight: 650;
  letter-spacing: 0.02em;
}
.brand-sub {
  font-size: var(--fs-11);
  color: var(--text-2);
  letter-spacing: 0.06em;
}

.nav-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.nav-label {
  font-size: var(--fs-11);
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--text-3);
  padding: 0 10px 6px;
}
.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  height: 38px;
  padding: 0 10px;
  border-radius: var(--radius-sm);
  color: var(--text-1);
  font-size: var(--fs-13);
  font-weight: 550;
  transition:
    background var(--dur) var(--ease),
    color var(--dur) var(--ease);
}
.nav-item:hover {
  background: var(--neutral-soft);
  color: var(--text-0);
}
/* 当前项：左侧 2px 黄铜竖条 + 极淡底色，比整块高亮更克制 */
.nav-item.active {
  background: var(--accent-soft);
  color: var(--text-0);
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: -6px;
  top: 8px;
  bottom: 8px;
  width: 2px;
  border-radius: var(--radius-pill);
  background: var(--accent);
}
.nav-item.active .nav-icon {
  color: var(--accent);
}
.nav-icon {
  width: 16px;
  text-align: center;
  color: var(--text-2);
  font-size: var(--fs-12);
  flex: 0 0 auto;
}
.nav-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-spacer {
  flex: 1 1 auto;
}

.nav-foot {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.health {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.quota-mini {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: rgba(243, 237, 227, 0.02);
}
.quota-label {
  font-size: var(--fs-11);
  color: var(--text-2);
  letter-spacing: 0.06em;
}
.quota-value {
  margin-left: auto;
  font-size: var(--fs-16);
  font-weight: 600;
  color: var(--text-0);
}
.quota-cap {
  color: var(--text-3);
}

/* 窄屏（手机 / 竖屏平板）：侧栏改为顶部横条，内容拿回全部宽度 */
@media (max-width: 900px) {
  .shell {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }
  .shell-nav {
    flex-direction: row;
    align-items: center;
    gap: var(--space-2);
    height: auto;
    padding: 8px 10px;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    overflow-x: auto;
    overflow-y: hidden;
  }
  .brand {
    padding: 0 10px 0 0;
    border-bottom: 0;
    flex: 0 0 auto;
  }
  .brand-sub,
  .nav-label,
  .nav-foot,
  .nav-spacer {
    display: none;
  }
  .nav-list {
    flex-direction: row;
    gap: 2px;
    flex: 1 1 auto;
  }
  .nav-item {
    height: 34px;
    padding: 0 10px;
    white-space: nowrap;
  }
  /* 横条上的当前项：下划线代替左侧竖条 */
  .nav-item.active::before {
    left: 10px;
    right: 10px;
    top: auto;
    bottom: 2px;
    width: auto;
    height: 2px;
  }
}

/* 主区是「壳」的容器：内部两栏按自身宽度收缩，而不是按视口 */
.shell-main {
  min-width: 0;
  height: 100%;
  overflow: auto;
  container-type: inline-size;
}
</style>
