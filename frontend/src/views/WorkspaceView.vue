<script setup lang="ts">
/**
 * 工作区落地页（PRD §5.1-1「登录 / 工作区页」）。
 * 展示创作者画像、工作区 ID、配额、概览计数与供应商状态，
 * 并提供进入项目列表 / 资产库两个入口。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { workspaceApi, type WorkspaceProfile } from '@/api/workspace'
import { formatCents } from '@/api/cost'
import { ApiError, toApiError } from '@/api/errors'
import { percent } from '@/utils/format'
import { useToastStore } from '@/stores/toast'
import ErrorState from '@/components/ErrorState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import StatTile from '@/components/StatTile.vue'
import QuotaMeter from '@/components/QuotaMeter.vue'

const toast = useToastStore()

const profile = ref<WorkspaceProfile | null>(null)
const loading = ref(false)
const loadError = ref<ApiError | null>(null)

/** 创作者名编辑态。 */
const editing = ref(false)
const saving = ref(false)
const draftName = ref('')
const saveError = ref('')
/** 保存成功后给一次行内反馈（toast 之外的就地确认）。 */
const savedHint = ref('')

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    profile.value = await workspaceApi.get()
  } catch (err) {
    loadError.value = toApiError(err)
  } finally {
    loading.value = false
  }
}

function startEdit(): void {
  if (!profile.value) return
  draftName.value = profile.value.user.name
  saveError.value = ''
  savedHint.value = ''
  editing.value = true
}

function cancelEdit(): void {
  editing.value = false
  saveError.value = ''
}

/** 保存显示名。失败时把后端返回的中文 message 就地展示，同时 api 层也会弹 toast。 */
async function saveName(): Promise<void> {
  const name = draftName.value.trim()
  if (!name) {
    saveError.value = '创作者名不能为空'
    return
  }
  saving.value = true
  saveError.value = ''
  try {
    const res = await workspaceApi.rename(name)
    if (profile.value) {
      // 后端对空白名会忽略，这里以返回值为准，避免前端显示与后端不一致
      profile.value.user.name = res.name
    }
    editing.value = false
    savedHint.value = `已保存为「${res.name}」`
    toast.success('创作者名已更新', res.name)
  } catch (err) {
    saveError.value =
      err instanceof ApiError ? err.message : '保存失败，请稍后重试'
  } finally {
    saving.value = false
  }
}

const quota = computed(() => profile.value?.quota ?? null)

/** 配额占用百分比，与 QuotaMeter 同口径（已用 + 预留）。 */
const usedPercent = computed(() => {
  const q = quota.value
  if (!q || !q.limit_cents) return 0
  return percent((q.used_cents ?? 0) + (q.reserved_cents ?? 0), q.limit_cents)
})

const barTone = computed(() => {
  if (usedPercent.value >= 95) return 'danger'
  if (usedPercent.value >= 75) return 'warn'
  return ''
})

const counts = computed(() => profile.value?.counts ?? null)

/** 概览卡片：顺序与 PRD §5.1-1 一致。 */
const countTiles = computed(() => {
  const c = counts.value
  if (!c) return []
  return [
    { label: '项目', value: c.projects, hint: `进行中 ${c.active_projects} · 已归档 ${c.archived_projects}` },
    { label: '剧集', value: c.episodes },
    { label: '任务', value: c.jobs },
    { label: '素材', value: c.media },
    { label: '角色', value: c.characters },
    { label: '画风', value: c.styles },
  ]
})

/** 演示模式：一个真实供应商密钥都没配。 */
const mockOnly = computed(() => profile.value?.providers?.mock_only === true)

const configuredProviders = computed(() => profile.value?.providers?.configured ?? [])

/** 供应商显示名；未知厂商原样透出，不臆造翻译。 */
const PROVIDER_LABEL: Record<string, string> = {
  ark: '火山方舟 ARK',
  kling: '可灵 Kling',
  minimax: 'MiniMax',
  openai: 'OpenAI',
}

function providerLabel(id: string): string {
  return PROVIDER_LABEL[id] ?? id
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">工作区</h1>
        <p class="muted small">
          本地创作者的个人制作空间。V1 不做多租户鉴权，所有项目归属同一工作区。
        </p>
      </div>
      <div class="row-wrap">
        <button type="button" class="btn btn-ghost" :disabled="loading" @click="load">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
        <RouterLink to="/" class="btn btn-primary">进入项目列表</RouterLink>
        <RouterLink to="/workspace/assets" class="btn">资产库</RouterLink>
      </div>
    </header>

    <SkeletonBlock v-if="loading && !profile" :rows="4" :height="18" />

    <ErrorState
      v-else-if="loadError && !profile"
      :error="loadError"
      context="加载工作区"
      @retry="load"
    />

    <template v-else-if="profile">
      <!-- 演示模式提示：未配置任何真实供应商密钥时显著提示 -->
      <div v-if="mockOnly" class="notice notice-warn" role="status">
        <span class="notice-mark" aria-hidden="true">!</span>
        <div class="notice-body">
          <div class="notice-title">当前为演示模式</div>
          <p class="notice-text">
            当前为演示模式（未配置任何真实供应商密钥），生成结果由 Mock 供应商产出。
            前往
            <RouterLink to="/settings" class="notice-link">设置</RouterLink>
            填写密钥后即可接入真实供应商。
          </p>
        </div>
      </div>

      <section class="card card-pad ws-identity">
        <div class="wi-head">
          <div class="wi-name-block">
            <div class="section-title">创作者</div>

            <div v-if="!editing" class="wi-name-row">
              <span class="wi-name" :title="profile.user.name">{{ profile.user.name }}</span>
              <button type="button" class="btn btn-sm btn-ghost" @click="startEdit">编辑</button>
            </div>

            <div v-else class="wi-edit">
              <input
                v-model="draftName"
                class="input wi-input"
                maxlength="120"
                placeholder="输入创作者名（最多 120 字）"
                :disabled="saving"
                @keyup.enter="saveName"
                @keyup.esc="cancelEdit"
              />
              <button type="button" class="btn btn-sm btn-primary" :disabled="saving" @click="saveName">
                {{ saving ? '保存中…' : '保存' }}
              </button>
              <button type="button" class="btn btn-sm btn-ghost" :disabled="saving" @click="cancelEdit">
                取消
              </button>
            </div>

            <p v-if="saveError" class="field-error">{{ saveError }}</p>
            <p v-else-if="savedHint" class="wi-saved">{{ savedHint }}</p>
            <p v-else class="hint">显示名保存后立即生效，其它页面与导出记录都会使用它。</p>
          </div>

          <!-- 工作区 / 用户 id 是内部标识，默认不铺在界面上，只在悬停时给出 -->
          <div
            class="wi-meta"
            :title="`工作区 ${profile.workspace_id} · 用户 ${profile.user.id}`"
          >
            <span class="badge badge-muted badge-dot">本地工作区</span>
            <span class="tiny dim">单人使用 · 项目均归属同一工作区</span>
          </div>
        </div>

        <div class="wi-defaults">
          <span class="tiny dim">新建默认值</span>
          <span class="chip mono">语言 {{ profile.defaults.language }}</span>
          <span class="chip mono">画幅 {{ profile.defaults.aspect_ratio }}</span>
        </div>
      </section>

      <section class="card card-pad">
        <div class="row-between sec-head">
          <span class="section-title">成本配额</span>
          <span class="mono tiny" :class="barTone === 'danger' ? 'wi-danger' : 'dim'">
            {{ formatCents(profile.quota.remaining_cents) }} 可用
          </span>
        </div>

        <div class="progress progress-lg">
          <div class="progress-bar" :class="barTone" :style="{ width: `${usedPercent}%` }" />
        </div>

        <div class="quota-grid mono tiny">
          <span>额度 {{ formatCents(profile.quota.limit_cents) }}</span>
          <span>已用 {{ formatCents(profile.quota.used_cents) }}</span>
          <span>已预留 {{ formatCents(profile.quota.reserved_cents) }}</span>
          <span>剩余 {{ formatCents(profile.quota.remaining_cents) }}</span>
          <span>占用 {{ usedPercent.toFixed(1) }}%</span>
        </div>

        <div class="wi-concurrent">
          <div class="row-between tiny dim">
            <span>并发任务</span>
            <span class="mono">{{ profile.quota.running_jobs }} / {{ profile.quota.concurrent_limit }}</span>
          </div>
          <div class="progress">
            <div
              class="progress-bar"
              :class="{ warn: profile.quota.concurrent_limit > 0 && profile.quota.running_jobs / profile.quota.concurrent_limit >= 0.8 }"
              :style="{
                width: `${percent(profile.quota.running_jobs, profile.quota.concurrent_limit)}%`,
              }"
            />
          </div>
        </div>

        <!-- 兼容既有组件：同一份配额数据的完整仪表（含骨架/异常兜底文案） -->
        <details class="quota-detail">
          <summary class="tiny dim">查看完整配额仪表</summary>
          <QuotaMeter :quota="quota" class="quota-meter-slot" />
        </details>
      </section>

      <section class="col">
        <div class="section-title">概览</div>
        <div v-if="countTiles.length" class="stat-row">
          <StatTile
            v-for="tile in countTiles"
            :key="tile.label"
            :label="tile.label"
            :value="tile.value"
            :hint="tile.hint ?? ''"
          />
        </div>
      </section>

      <section class="card card-pad">
        <div class="row-between sec-head">
          <span class="section-title">供应商</span>
          <span class="badge" :class="mockOnly ? 'badge-warn' : 'badge-ok'">
            {{ mockOnly ? '仅 Mock' : `已配置 ${configuredProviders.length} 个` }}
          </span>
        </div>

        <p v-if="mockOnly" class="tiny dim">
          未检测到任何真实供应商密钥，所有生成均走 Mock 供应商，产出为占位结果。
        </p>

        <div v-else class="row-wrap">
          <span v-for="id in configuredProviders" :key="id" class="chip">{{ providerLabel(id) }}</span>
          <span v-if="!configuredProviders.length" class="tiny dim">后端未返回已配置供应商清单。</span>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.sec-head {
  margin-bottom: 10px;
}

/* 演示模式提示条 */
.notice {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  border: 1px solid var(--line);
}
.notice-warn {
  border-color: rgba(232, 203, 109, 0.35);
  background: linear-gradient(180deg, rgba(232, 203, 109, 0.08), rgba(232, 203, 109, 0.02));
}
.notice-mark {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-size: var(--fs-12);
  font-weight: 700;
  background: var(--warn-soft);
  color: var(--warn);
  border: 1px solid rgba(232, 203, 109, 0.4);
  flex: 0 0 auto;
  margin-top: 1px;
}
.notice-body {
  min-width: 0;
}
.notice-title {
  font-size: var(--fs-13);
  font-weight: 650;
  color: var(--warn);
}
.notice-text {
  margin-top: 3px;
  font-size: var(--fs-12);
  line-height: 1.6;
  color: var(--text-1);
}
.notice-link {
  color: var(--accent);
  text-decoration: underline;
}

/* 身份卡 */
.ws-identity {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.wi-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-5);
  flex-wrap: wrap;
}
.wi-name-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}
.wi-name-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.wi-name {
  font-size: var(--fs-24);
  font-weight: 680;
  letter-spacing: -0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.wi-edit {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.wi-input {
  width: 260px;
}
.wi-saved {
  font-size: var(--fs-11);
  color: var(--ok);
}
.wi-meta {
  display: flex;
  gap: var(--space-6);
  flex-wrap: wrap;
}
.wi-meta-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.ws-id {
  font-size: var(--fs-14);
  font-weight: 600;
  color: var(--text-0);
}
.wi-defaults {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

/* 配额 */
.quota-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) 14px;
  margin-top: 8px;
  color: var(--text-2);
}
.wi-concurrent {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: 10px;
}
.wi-danger {
  color: var(--danger);
}
.quota-detail {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.quota-detail summary {
  cursor: pointer;
}
.quota-meter-slot {
  margin-top: 10px;
}

.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--gap);
}
</style>
