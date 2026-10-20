<script setup lang="ts">
/**
 * 设置：供应商密钥与配额（API.md §1、§13、§14）。
 * 密钥只回显 `configured` 与 `masked` 前缀；提交时才发送明文。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import {
  PROVIDER_SLOTS,
  SLOT_LABEL,
  emptySlot,
  settingsApi,
  type ProviderSlot,
} from '@/api/settings'
import { formatCents } from '@/api/cost'
import { formatDateTime } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ErrorState from '@/components/ErrorState.vue'
import StatTile from '@/components/StatTile.vue'
import QuotaMeter from '@/components/QuotaMeter.vue'
import type { ApiError } from '@/api/errors'
import type { PricingEntry, ProviderInfo, ProviderSlotConfig, SafetyResult, SettingsProviders } from '@/types'

const config = useConfigStore()
const toast = useToastStore()
const { providers, limits, quota } = storeToRefs(config)

/** 表单模型：四个槽位始终存在，避免模板里出现非空断言。 */
const form = reactive<Record<ProviderSlot, ProviderSlotConfig>>({
  text: emptySlot(),
  image: emptySlot(),
  video: emptySlot(),
  speech: emptySlot(),
})
const loading = ref(false)
const saving = ref(false)
const loadError = ref<ApiError | null>(null)

const pricing = ref<PricingEntry[]>([])
const pricingVersion = ref<string | null>(null)

const safetyText = ref('')
const safetyResult = ref<SafetyResult | null>(null)
const safetyBusy = ref(false)

const limitsRows = computed(() => [
  { label: '最大并发任务', value: String(limits.value.max_concurrent_jobs ?? '—') },
  { label: '默认候选数 / 镜', value: String(limits.value.candidates_per_shot_default ?? '—') },
  { label: '候选数上限 / 镜', value: String(limits.value.candidates_per_shot_max ?? '—') },
  {
    label: '单镜时长范围',
    value: `${limits.value.shot_duration_ms_min ?? '—'} ~ ${limits.value.shot_duration_ms_max ?? '—'} ms`,
  },
  { label: '剧集时长上限', value: `${limits.value.episode_duration_ms_max ?? '—'} ms` },
  { label: '预览码率', value: `${limits.value.preview_bitrate_kbps ?? '—'} kbps` },
  { label: '导出预设', value: (limits.value.export_presets ?? []).map((p) => (typeof p === 'string' ? p : (p.id ?? p.label))).join('、') || '—' },
  { label: '支持画幅', value: (limits.value.aspect_ratios ?? []).join('、') || '—' },
  { label: '默认语言', value: String(limits.value.default_language ?? '—') },
])

function modelsOf(provider?: ProviderInfo): { id: string; label: string }[] {
  if (!provider?.models) return []
  return provider.models.map((m) =>
    typeof m === 'string' ? { id: m, label: m } : { id: m.id, label: m.display_name ?? m.id },
  )
}

function providerOptions(slot: ProviderSlot): ProviderInfo[] {
  return config.providersByKind(slot)
}

function currentProvider(slot: ProviderSlot): ProviderInfo | undefined {
  const id = form[slot].provider
  return providers.value.find((p) => p.id === id)
}

async function loadSettings(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const remote = await settingsApi.getProviders()
    for (const slot of PROVIDER_SLOTS) {
      const slotConfig = remote[slot] ?? emptySlot()
      form[slot] = { ...emptySlot(), ...slotConfig, api_key: '' }
    }
    const table = await settingsApi.pricing().catch(() => null)
    pricing.value = table?.entries ?? table?.items ?? []
    pricingVersion.value = table?.version ?? null
  } catch (err) {
    loadError.value = err as ApiError
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await config.load()
  await Promise.all([loadSettings(), config.loadQuota()])
})

async function save(): Promise<void> {
  saving.value = true
  try {
    const payload: SettingsProviders = {}
    for (const slot of PROVIDER_SLOTS) {
      const value = form[slot]
      if (!value?.provider) continue
      payload[slot] = {
        provider: value.provider,
        model: value.model || undefined,
        base_url: value.base_url || undefined,
        // 只有用户真正输入了密钥才提交，避免把空串覆盖已有密钥
        api_key: value.api_key ? value.api_key : undefined,
        // FR-090：留空即表示清除备用供应商配置
        fallback_provider: value.fallback_provider || '',
        fallback_model: value.fallback_model || undefined,
        fallback_base_url: value.fallback_base_url || undefined,
        fallback_api_key: value.fallback_api_key ? value.fallback_api_key : undefined,
      }
    }
    const updated = await settingsApi.putProviders(payload)
    for (const slot of PROVIDER_SLOTS) {
      const slotConfig = updated[slot] ?? emptySlot()
      form[slot] = { ...emptySlot(), ...slotConfig, api_key: '' }
    }
    toast.success('供应商配置已保存', '密钥已由服务端加密存储')
  } catch {
    /* api 层已提示 */
  } finally {
    saving.value = false
  }
}

async function runSafetyCheck(): Promise<void> {
  if (!safetyText.value.trim()) {
    toast.warn('请输入需要检查的文本')
    return
  }
  safetyBusy.value = true
  try {
    safetyResult.value = await settingsApi.safetyCheck(safetyText.value)
  } catch {
    safetyResult.value = null
  } finally {
    safetyBusy.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">设置</h1>
        <p class="muted small">
          供应商密钥、配额与限制。所有限制值均来自后端 <span class="mono">/config/limits</span>，前端不写死。
        </p>
      </div>
      <div class="row-wrap">
        <button type="button" class="btn btn-ghost" :disabled="loading" @click="loadSettings">重新加载</button>
        <button type="button" class="btn btn-primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存供应商配置' }}
        </button>
      </div>
    </header>

    <div class="stat-row">
      <StatTile label="并发上限" :value="limits.max_concurrent_jobs ?? '—'" hint="来自 /config/limits" />
      <StatTile label="运行中任务" :value="quota?.running_jobs ?? '—'" />
      <StatTile label="剩余配额" :value="quota ? formatCents(quota.remaining_cents) : '—'" tone="accent" />
      <StatTile label="价格版本" :value="pricingVersion ?? '—'" />
    </div>

    <ErrorState v-if="loadError" :error="loadError" context="加载设置" @retry="loadSettings" />

    <SkeletonBlock v-if="loading && !providers.length" variant="cards" :rows="4" />

    <section v-else class="card">
      <div class="card-head">
        <h2 class="title-md">供应商与密钥</h2>
        <span class="tiny dim">密钥只回显前缀；留空表示不修改</span>
      </div>
      <div class="card-body provider-grid">
        <div v-for="slot in PROVIDER_SLOTS" :key="slot" class="provider-slot">
          <div class="row-between">
            <span class="section-title">{{ SLOT_LABEL[slot] }}</span>
            <span
              class="badge"
              :class="form[slot].configured ? 'badge-ok' : 'badge-warn'"
            >
              {{ form[slot].configured ? '已配置' : '未配置' }}
            </span>
          </div>

          <div class="field">
            <label class="label" :for="`p-${slot}`">供应商</label>
            <select :id="`p-${slot}`" v-model="form[slot].provider" class="select">
              <option value="">选择供应商…</option>
              <option v-for="provider in providerOptions(slot)" :key="provider.id" :value="provider.id">
                {{ provider.display_name || provider.id }}
              </option>
            </select>
            <span v-if="!providerOptions(slot).length" class="hint">
              /config/providers 未返回该模态的供应商
            </span>
          </div>

          <div class="field">
            <label class="label" :for="`m-${slot}`">模型</label>
            <select
              v-if="modelsOf(currentProvider(slot)).length"
              :id="`m-${slot}`"
              v-model="form[slot].model"
              class="select"
            >
              <option value="">默认模型</option>
              <option v-for="model in modelsOf(currentProvider(slot))" :key="model.id" :value="model.id">
                {{ model.label }}
              </option>
            </select>
            <input
              v-else
              :id="`m-${slot}`"
              v-model="form[slot].model"
              class="input mono"
              placeholder="模型 ID（可选）"
            />
          </div>

          <div class="field">
            <label class="label" :for="`k-${slot}`">API Key</label>
            <input
              :id="`k-${slot}`"
              v-model="form[slot].api_key"
              class="input mono"
              type="password"
              autocomplete="off"
              :placeholder="form[slot].masked ? `已保存：${form[slot].masked}` : 'sk-...'"
            />
          </div>

          <div class="field">
            <label class="label" :for="`b-${slot}`">Base URL</label>
            <input :id="`b-${slot}`" v-model="form[slot].base_url" class="input mono" placeholder="可选" />
          </div>

          <!-- FR-090 备用供应商：主供应商超时/限流/不可用时自动切换 -->
          <div class="fallback-block">
            <div class="row-between">
              <span class="label">备用供应商</span>
              <span
                class="badge"
                :class="form[slot].fallback_provider
                  ? (form[slot].fallback_configured ? 'badge-ok' : 'badge-warn')
                  : 'badge-muted'"
              >
                {{
                  form[slot].fallback_provider
                    ? (form[slot].fallback_configured ? '已配置' : '未配置密钥')
                    : '未启用'
                }}
              </span>
            </div>
            <div class="field">
              <select :id="`fb-${slot}`" v-model="form[slot].fallback_provider" class="select">
                <option value="">不配置备用</option>
                <option
                  v-for="provider in providerOptions(slot)"
                  :key="provider.id"
                  :value="provider.id"
                  :disabled="provider.id === form[slot].provider"
                >
                  {{ provider.display_name || provider.id }}
                  {{ provider.id === form[slot].provider ? '（与主供应商相同）' : '' }}
                </option>
              </select>
              <span class="hint">主供应商返回超时 / 限流 / 不可用时自动改用它重试一次</span>
            </div>
            <template v-if="form[slot].fallback_provider">
              <div class="field">
                <label class="label" :for="`fbm-${slot}`">备用模型</label>
                <input
                  :id="`fbm-${slot}`"
                  v-model="form[slot].fallback_model"
                  class="input mono"
                  placeholder="留空用供应商默认"
                />
              </div>
              <div class="field">
                <label class="label" :for="`fbk-${slot}`">备用 API Key</label>
                <input
                  :id="`fbk-${slot}`"
                  v-model="form[slot].fallback_api_key"
                  class="input mono"
                  type="password"
                  autocomplete="off"
                  :placeholder="form[slot].fallback_masked
                    ? `已保存：${form[slot].fallback_masked}`
                    : 'sk-...'"
                />
              </div>
              <div class="field">
                <label class="label" :for="`fbb-${slot}`">备用 Base URL</label>
                <input
                  :id="`fbb-${slot}`"
                  v-model="form[slot].fallback_base_url"
                  class="input mono"
                  placeholder="可选"
                />
              </div>
            </template>
          </div>
        </div>
      </div>
    </section>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">配额</h2>
          <button type="button" class="btn btn-sm btn-ghost" @click="config.loadQuota()">刷新</button>
        </div>
        <div class="card-body">
          <QuotaMeter :quota="quota" />
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">限制项（只读）</h2>
          <span class="tiny dim mono">/config/limits</span>
        </div>
        <div class="card-body">
          <dl class="kv">
            <template v-for="row in limitsRows" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd class="mono">{{ row.value }}</dd>
            </template>
          </dl>
        </div>
      </section>
    </div>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">价格表</h2>
          <span class="mono tiny dim">{{ pricing.length }} 条</span>
        </div>
        <div class="card-body">
          <EmptyState
            v-if="!pricing.length"
            compact
            icon="¥"
            title="暂无价格数据"
            description="/settings/pricing 未返回条目。"
          />
          <table v-else class="table">
            <thead>
              <tr>
                <th>类型</th>
                <th>供应商</th>
                <th>模型</th>
                <th>单价</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(entry, i) in pricing" :key="i">
                <td class="tiny">{{ entry.kind ?? '—' }}</td>
                <td class="mono tiny">{{ entry.provider ?? '—' }}</td>
                <td class="mono tiny">{{ entry.model ?? '—' }}</td>
                <td class="mono tiny">
                  {{ entry.unit_price_cents != null ? formatCents(entry.unit_price_cents) : '—' }}
                  <span class="dim">{{ entry.unit ?? '' }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">安全检查</h2>
          <button type="button" class="btn btn-sm" :disabled="safetyBusy" @click="runSafetyCheck">
            {{ safetyBusy ? '检查中…' : '检查文本' }}
          </button>
        </div>
        <div class="card-body col" style="gap: 12px">
          <div class="field">
            <label class="label" for="safe-text">待检查文本</label>
            <textarea
              id="safe-text"
              v-model="safetyText"
              class="textarea"
              rows="4"
              placeholder="粘贴剧本文本，提交 /safety/check 预览风险等级"
            />
          </div>
          <div v-if="safetyResult" class="safety-result" :class="safetyResult.allowed ? 'ok' : 'bad'">
            <div class="row-wrap">
              <span class="badge" :class="safetyResult.allowed ? 'badge-ok' : 'badge-danger'">
                {{ safetyResult.allowed ? '允许' : '拦截' }}
              </span>
              <span v-if="safetyResult.risk_level" class="badge badge-warn">
                风险等级 {{ safetyResult.risk_level }}
              </span>
            </div>
            <div v-if="safetyResult.categories?.length" class="row-wrap">
              <span v-for="category in safetyResult.categories" :key="category" class="chip">{{ category }}</span>
            </div>
            <ul v-if="safetyResult.suggestions?.length" class="safety-list">
              <li v-for="(s, i) in safetyResult.suggestions" :key="i" class="tiny">{{ s }}</li>
            </ul>
          </div>
          <p v-else class="tiny dim">
            最近检查时间：{{ formatDateTime(new Date().toISOString()) }}（提交后显示结果）
          </p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: var(--gap-lg);
  max-width: 1500px;
}
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.page-head p {
  margin-top: 4px;
  max-width: 660px;
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--gap);
}
.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--gap);
}
/* FR-090 备用供应商：与主供应商配置视觉分层，默认收起以保持卡片紧凑 */
.fallback-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 4px;
  padding-top: 12px;
  border-top: 1px dashed var(--line);
}
.provider-slot {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
}
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
  align-items: start;
}
@media (max-width: 1120px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.safety-result {
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--line);
}
.safety-result.ok {
  border-color: rgba(52, 211, 153, 0.35);
  background: var(--ok-soft);
}
.safety-result.bad {
  border-color: rgba(248, 113, 113, 0.35);
  background: var(--danger-soft);
}
.safety-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  color: var(--text-1);
}
</style>
